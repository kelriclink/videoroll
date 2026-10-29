from __future__ import annotations

import contextlib
import pytest
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from videoroll.utils.auto_youtube import encode_auto_youtube_created_by

from videoroll.apps.subtitle_service import worker
from videoroll.apps.subtitle_service.provider_rate_limit import ProviderGateTimeout


def test_worker_has_no_rss_memory_limit() -> None:
    assert worker.celery_app.conf.worker_max_memory_per_child is None
    assert worker.celery_app.conf.worker_max_tasks_per_child == 20


def test_remaining_celery_worker_is_control_plane_only() -> None:
    from videoroll.apps.subtitle_service.queues import SUBTITLE_CONTROL_QUEUE

    assert worker.celery_app.conf.worker_prefetch_multiplier == 1
    schedules = dict(worker.celery_app.conf.beat_schedule)
    assert schedules
    assert {item["options"]["queue"] for item in schedules.values()} == {SUBTITLE_CONTROL_QUEUE}


def test_runtime_profile_marker_preserves_manual_override_on_automatic_task() -> None:
    task = SimpleNamespace(created_by=encode_auto_youtube_created_by("auto_youtube", auto_publish=None))

    assert worker._uses_runtime_auto_profile(task, {}) is True
    assert worker._uses_runtime_auto_profile(task, {"runtime_profile": True}) is True
    assert worker._uses_runtime_auto_profile(task, {"runtime_profile": False}) is False
    assert worker._uses_runtime_auto_profile(task, {}, {"runtime_profile": False}) is False


def test_active_pipeline_job_prefers_existing_subtitle_and_avoids_render_lookup() -> None:
    task_id = uuid.uuid4()
    subtitle_id = uuid.uuid4()
    subtitle = SimpleNamespace(id=subtitle_id)
    subtitle_query = Mock()
    subtitle_query.filter.return_value.order_by.return_value.first.return_value = subtitle
    db = Mock()
    db.query.return_value = subtitle_query

    assert worker._active_pipeline_job(db, task_id) == ("subtitle", subtitle_id)
    db.query.assert_called_once_with(worker.SubtitleJob)


def test_active_pipeline_job_reuses_render_when_no_subtitle_is_active() -> None:
    task_id = uuid.uuid4()
    render_id = uuid.uuid4()
    subtitle_query = Mock()
    subtitle_query.filter.return_value.order_by.return_value.first.return_value = None
    render_query = Mock()
    render_query.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(id=render_id)
    db = Mock()
    db.query.side_effect = [subtitle_query, render_query]

    assert worker._active_pipeline_job(db, task_id) == ("render", render_id)


@pytest.mark.parametrize("override,expected", [(0, 8), (20, 8), (2, 2)])
def test_cpu_threads_share_the_hatchet_subtitle_slot_budget(monkeypatch: pytest.MonkeyPatch, override: int, expected: int) -> None:
    monkeypatch.setenv("HATCHET_SUBTITLE_WORKER_SLOTS", "2")
    monkeypatch.setattr(worker.settings, "whisper_cpu_threads", override)
    monkeypatch.setattr(worker.settings, "whisper_num_workers", 1)
    monkeypatch.setattr(worker, "process_cpu_count", lambda: 16)

    assert worker._asr_cpu_threads(object()) == expected


def test_openvino_cpu_does_not_take_gpu_resource_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "_db", lambda: (_ for _ in ()).throw(AssertionError("GPU lock DB should not be used")))

    with worker._openvino_gpu_slot(device="CPU", log_path=None):
        pass


def test_openvino_gpu_resource_lock_waits_without_an_idle_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ScalarResult:
        def __init__(self, value: bool) -> None:
            self.value = value

        def scalar(self) -> bool:
            return self.value

    class _LockConn:
        def __init__(self, engine) -> None:
            self.engine = engine
            self.closed = False
            self.invalidated = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def execute(self, statement, _params):
            sql = str(statement)
            self.engine.statements.append(sql)
            if "pg_try_advisory_lock" in sql:
                return _ScalarResult(next(self.engine.results))
            if "pg_advisory_unlock" in sql:
                return _ScalarResult(True)
            raise AssertionError(sql)

        def invalidate(self):
            self.invalidated = True

    class _LockEngine:
        def __init__(self) -> None:
            self.results = iter([False, True])
            self.connections = []
            self.statements = []

        def execution_options(self, **kwargs):
            assert kwargs == {"isolation_level": "AUTOCOMMIT"}
            return self

        def connect(self):
            conn = _LockConn(self)
            self.connections.append(conn)
            return conn

    engine = _LockEngine()
    cancel_checks = 0

    def cancel_check() -> None:
        nonlocal cancel_checks
        cancel_checks += 1

    monkeypatch.setattr(worker, "get_engine", lambda _url: engine)
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("OPENVINO_GPU_MAX_CONCURRENCY", "1")

    with worker._openvino_gpu_slot(device="GPU", log_path=None, cancel_check=cancel_check):
        assert len(engine.connections) == 2
        assert engine.connections[0].closed
        assert not engine.connections[1].closed

    assert cancel_checks >= 2
    assert all(connection.closed for connection in engine.connections)
    assert sum("pg_try_advisory_lock" in sql for sql in engine.statements) == 2
    assert sum("pg_advisory_unlock" in sql for sql in engine.statements) == 1
    assert all("xact" not in sql for sql in engine.statements)


def test_openvino_gpu_resource_lock_unlocks_when_inference_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.scalar.return_value = True
    engine = MagicMock()
    engine.execution_options.return_value.connect.return_value = conn
    monkeypatch.setattr(worker, "get_engine", lambda _url: engine)
    monkeypatch.setenv("OPENVINO_GPU_MAX_CONCURRENCY", "1")

    with pytest.raises(RuntimeError, match="inference failed"):
        with worker._openvino_gpu_slot(device="GPU", log_path=None):
            raise RuntimeError("inference failed")

    assert conn.execute.call_count == 2  # acquire then unlock, including error path
    assert "pg_advisory_unlock" in str(conn.execute.call_args.args[0])
    conn.invalidate.assert_not_called()


def test_openvino_gpu_aliases_share_one_lock_key() -> None:
    assert worker._openvino_gpu_lock_key("GPU", 0) == worker._openvino_gpu_lock_key("GPU.0", 0)
    assert worker._openvino_gpu_lock_key("AUTO", 0) == worker._openvino_gpu_lock_key("GPU.0", 0)
    assert worker._openvino_gpu_lock_key("GPU.1", 0) != worker._openvino_gpu_lock_key("GPU.0", 0)


@pytest.mark.parametrize("error,transient", [
    (RuntimeError("Infer Request is busy"), True),
    (RuntimeError("terminating connection due to idle-in-transaction timeout"), True),
    (ProviderGateTimeout("groq-whisper local concurrency wait budget exceeded"), True),
    (ValueError("missing input.key"), False),
    (RuntimeError("model does not support language"), False),
])
def test_only_known_temporary_subtitle_errors_are_retryable(error: Exception, transient: bool) -> None:
    assert worker._is_transient_subtitle_error(error) is transient


def test_run_asr_stage_gates_only_openvino_gpu(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    entered: list[str] = []

    @contextlib.contextmanager
    def fake_slot(*, device: str, log_path, cancel_check=None):
        entered.append(device)
        yield

    monkeypatch.setattr(worker, "_openvino_gpu_slot", fake_slot)
    monkeypatch.setattr(
        worker,
        "get_asr_settings",
        lambda _db, _settings: {
            "default_engine": "openvino",
            "default_language": "auto",
            "default_model": "/models/large-v3",
            "openvino_device": "GPU",
            "openvino_num_beams": 1,
            "openvino_max_new_tokens": 32,
            "openvino_vad_enabled": False,
            "openvino_vad_threshold": 0.5,
            "model_download_proxy": "",
        },
    )
    monkeypatch.setattr(worker, "_resolve_openvino_model", lambda model, _root, proxy=None: model)
    monkeypatch.setattr(worker, "transcribe_openvino_whisper", lambda *args, **kwargs: [])

    db = Mock()
    db.new = db.dirty = db.deleted = ()
    result = worker._run_asr_stage(
        db=db,
        audio_path=tmp_path / "audio.wav",
        audio_key=None,
        asr_cfg={"engine": "openvino"},
        log_path=None,
        groq_checkpoint_path=tmp_path / "groq.json",
    )

    assert result == []
    assert entered == ["GPU"]
