from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from videoroll.apps.subtitle_service import main as subtitle_api, worker
from videoroll.apps.subtitle_service.render_queue_store import TASK_QUEUE_SETTINGS_KEY
from videoroll.config import SubtitleServiceSettings
from videoroll.db.base import Base
from videoroll.db.models import AppSetting, RenderJob, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task
from videoroll.utils import resources


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def _stats(total_mb: int, available_mb: int) -> dict[str, int]:
    return {"total_bytes": total_mb * 1024**2, "available_bytes": available_mb * 1024**2}


@pytest.fixture
def queue_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, AppSetting.__table__, SubtitleJob.__table__, RenderJob.__table__])
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    # SQLite drops timezone information; model real PostgreSQL UTC timestamps.
    @event.listens_for(sessions, "loaded_as_persistent")
    def _utc_dates(_session: Session, instance: object) -> None:
        for name in ("lock_until", "lease_until", "heartbeat_at", "updated_at", "created_at"):
            value = getattr(instance, name, None)
            if isinstance(value, datetime) and value.tzinfo is None:
                set_committed_value(instance, name, value.replace(tzinfo=timezone.utc))

    config = SubtitleServiceSettings(
        _env_file=None,
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://127.0.0.1:1/0",
        SUBTITLE_ASR_ENGINE="openvino",
        SUBTITLE_OPENVINO_MODEL="whisper-large-v3-fp16-ov",
        STORAGE_ROOT=str(tmp_path / "storage"),
        WORK_DIR=str(tmp_path / "work"),
    )
    memory = {"host": _stats(16 * 1024, 14 * 1024), "cgroup": None}
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(worker, "settings", config)
    monkeypatch.setattr(subtitle_api, "get_subtitle_settings", lambda: config)
    monkeypatch.setattr(worker, "_db", sessions)
    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(resources, "read_memory_stats", lambda: memory["host"])
    monkeypatch.setattr(resources, "read_cgroup_memory_stats", lambda: memory["cgroup"])
    monkeypatch.setattr(resources, "_read_text", lambda _path: None)
    monkeypatch.setattr("videoroll.realtime.publish_ui_event", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "publish_queue_changed", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.celery_app, "send_task", lambda name, **kwargs: sent.append((name, kwargs)))
    with sessions() as db:
        db.add(AppSetting(key=TASK_QUEUE_SETTINGS_KEY, value_json={"max_concurrency": 2}))
        db.commit()
    yield sessions, memory, sent
    engine.dispose()


def _enqueue(sessions, *, model: str = "large-v3", engine: str = "openvino", progress: int = 0,
             running: bool = False, task_lock: bool = False):
    now = datetime.now(timezone.utc)
    with sessions() as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        if task_lock:
            task.lock_owner = worker.TASK_QUEUE_LOCK_OWNER
            task.lock_until = now + timedelta(minutes=5)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id,
            status=SubtitleJobStatus.running if running else SubtitleJobStatus.queued,
            request_json={"asr": {"engine": engine, "model": model}},
            progress=progress,
            updated_at=now,
            lease_owner="existing-worker" if running else None,
            lease_until=now + timedelta(minutes=10) if running else None,
        )
        db.add(job)
        db.commit()
        return task.id, job.id


def _subtitle_dispatches(sent):
    return [options["args"][0] for name, options in sent if name == "subtitle_service.process_job"]


@pytest.mark.parametrize("total_mb,available_mb,expected", [(16384, 14395, 1), (32768, 28672, 2), (16384, 7000, 0)])
def test_large_asr_admission_respects_memory_without_rewriting_user_limit(queue_env, total_mb, available_mb, expected):
    sessions, memory, sent = queue_env
    memory["host"] = _stats(total_mb, available_mb)
    _enqueue(sessions)
    _enqueue(sessions)

    worker.task_queue_tick.run()

    assert len(_subtitle_dispatches(sent)) == expected
    with sessions() as db:
        assert db.get(AppSetting, TASK_QUEUE_SETTINGS_KEY).value_json["max_concurrency"] == 2


def test_next_tick_reserves_a_dispatched_job_before_worker_has_allocated_memory(queue_env):
    sessions, _memory, sent = queue_env
    with sessions() as db:
        db.get(AppSetting, TASK_QUEUE_SETTINGS_KEY).value_json = {"max_concurrency": 1}
        db.commit()
    _enqueue(sessions)
    _enqueue(sessions)
    worker.task_queue_tick.run()
    assert len(_subtitle_dispatches(sent)) == 1
    with sessions() as db:
        db.get(AppSetting, TASK_QUEUE_SETTINGS_KEY).value_json = {"max_concurrency": 2}
        db.commit()

    worker.task_queue_tick.run()

    assert len(_subtitle_dispatches(sent)) == 1


def test_scheduler_uses_the_smaller_cgroup_budget(queue_env):
    sessions, memory, sent = queue_env
    memory["host"] = _stats(65536, 60000)
    memory["cgroup"] = _stats(16384, 14395)
    _enqueue(sessions)
    _enqueue(sessions)

    worker.task_queue_tick.run()

    assert len(_subtitle_dispatches(sent)) == 1


def test_unknown_host_memory_keeps_local_asr_queued(queue_env):
    sessions, memory, sent = queue_env
    memory["host"] = None
    _enqueue(sessions)
    _enqueue(sessions)

    worker.task_queue_tick.run()

    assert _subtitle_dispatches(sent) == []


def test_tiny_models_do_not_inherit_the_large_model_budget(queue_env):
    sessions, memory, sent = queue_env
    memory["host"] = _stats(8192, 7000)
    _enqueue(sessions, engine="faster-whisper", model="tiny")
    _enqueue(sessions, engine="faster-whisper", model="tiny.en")

    worker.task_queue_tick.run()

    assert len(_subtitle_dispatches(sent)) == 2


def test_external_asr_does_not_reserve_a_local_large_model(queue_env):
    sessions, memory, sent = queue_env
    memory["host"] = None
    _enqueue(sessions, engine="external-whisper")
    _enqueue(sessions, engine="groq-whisper")

    worker.task_queue_tick.run()

    assert len(_subtitle_dispatches(sent)) == 2


def test_memory_pressure_keeps_live_jobs_untouched_and_occupying_capacity(queue_env):
    sessions, memory, sent = queue_env
    memory["host"] = _stats(16384, 500)
    task_id, job_id = _enqueue(sessions, running=True, progress=20)
    _enqueue(sessions)

    worker.task_queue_tick.run()

    assert _subtitle_dispatches(sent) == []
    with sessions() as db:
        live = db.get(SubtitleJob, job_id)
        assert live.status == SubtitleJobStatus.running
        assert live.lease_owner == "existing-worker"
        assert live.progress == 20
        assert db.get(Task, task_id).lock_owner is None


def test_queue_api_reports_memory_wait_separately_from_saved_concurrency(queue_env):
    sessions, _memory, _sent = queue_env
    _enqueue(sessions)
    _enqueue(sessions)
    worker.task_queue_tick.run()

    with sessions() as db:
        response = subtitle_api._read_task_queue(db, limit=200)

    assert response.settings.max_concurrency == 2
    assert response.admission.effective_max_concurrency == 1
    assert response.admission.local_asr_memory_mb == 8192
    assert response.admission.scope == "scheduler"
    assert any(item.waiting_reason and "内存" in item.waiting_reason for item in response.tasks)


def test_execution_worker_rechecks_its_own_available_memory_before_claiming(queue_env, monkeypatch):
    sessions, memory, _sent = queue_env
    task_id, job_id = _enqueue(sessions, progress=1, task_lock=True)
    memory["host"] = _stats(65536, 60000)
    memory["cgroup"] = _stats(8192, 7000)
    store = MagicMock()
    monkeypatch.setattr(worker, "FileStore", lambda _settings: store)
    task_heartbeat = MagicMock()
    monkeypatch.setattr(worker, "_TaskQueueHeartbeat", task_heartbeat)
    monkeypatch.setattr(worker, "JobLeaseHeartbeat", MagicMock())

    result = worker.process_job.run(str(job_id))

    assert result["status"] == "queued"
    assert "内存" in result["detail"]
    task_heartbeat.assert_not_called()
    with sessions() as db:
        job = db.get(SubtitleJob, job_id)
        assert job.status == SubtitleJobStatus.queued
        assert job.lease_owner is None
        assert job.progress == 0
        assert db.get(Task, task_id).lock_owner is None
        response = subtitle_api._read_task_queue(db, limit=200)
        assert any(item.waiting_reason and "执行容器" in item.waiting_reason for item in response.tasks)


def test_subtitle_workers_recycle_high_rss_children_only_after_task_completion() -> None:
    assert worker.celery_app.conf.worker_max_memory_per_child == 1536 * 1024
    assert worker.celery_app.conf.worker_max_tasks_per_child == 20


@pytest.mark.parametrize("override,expected", [(0, 8), (20, 8), (2, 2)])
def test_cpu_threads_share_the_task_concurrency_budget(queue_env, monkeypatch, override, expected):
    sessions, _memory, _sent = queue_env
    monkeypatch.setattr(worker.settings, "whisper_cpu_threads", override)
    monkeypatch.setattr(worker, "process_cpu_count", lambda: 16)

    with sessions() as db:
        assert worker._asr_cpu_threads(db) == expected
