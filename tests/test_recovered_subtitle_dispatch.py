from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from videoroll.apps.orchestrator_api.infrastructure import scheduler as scheduler_module
from videoroll.apps.subtitle_service.worker_concurrency import RECOVERY_RELAUNCH_PENDING_KEY
from videoroll.db.base import Base
from videoroll.db.models import PipelineRun, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task
from videoroll.workflows.launcher import SUBTITLE_JOB_WORKFLOW_NAME


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__, PipelineRun.__table__])
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


def test_recovered_subtitle_dispatch_supersedes_stale_run_and_relaunches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = _database()
    now = datetime.now(timezone.utc)
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id,
            status=SubtitleJobStatus.queued,
            request_json={"resume": True, RECOVERY_RELAUNCH_PENDING_KEY: True},
            updated_at=now,
        )
        db.add(job)
        db.flush()
        job_id = job.id
        stale = PipelineRun(
            task_id=task.id,
            engine="hatchet",
            workflow_name=SUBTITLE_JOB_WORKFLOW_NAME,
            external_run_id="old-run",
            state="submitted",
            request_json={"job_id": str(job.id), "priority": 0},
            created_at=now - timedelta(minutes=10),
            updated_at=now - timedelta(minutes=10),
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

    launched: list[object] = []

    class FakeLauncher:
        def __init__(self, _settings: object) -> None:
            pass

        def launch_subtitle_job(self, recovered_job_id, **_kwargs):
            launched.append(recovered_job_id)
            return SimpleNamespace(external_run_id="replacement-run")

    monkeypatch.setattr(scheduler_module, "get_sessionmaker", lambda _url: factory)
    monkeypatch.setattr(scheduler_module, "PipelineLauncher", FakeLauncher)
    scheduler = scheduler_module.OrchestratorScheduler(SimpleNamespace(database_url="sqlite://"))

    assert scheduler._dispatch_recovered_subtitle_jobs_once() == 1
    assert launched == [job_id]

    with factory() as db:
        persisted_job = db.get(SubtitleJob, job_id)
        persisted_stale = db.get(PipelineRun, stale_id)
        assert persisted_job is not None
        assert RECOVERY_RELAUNCH_PENDING_KEY not in persisted_job.request_json
        assert persisted_stale is not None
        assert persisted_stale.state == "superseded"
        assert persisted_stale.finished_at is not None

    engine.dispose()


def test_recovered_subtitle_dispatch_does_not_duplicate_recent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = _database()
    now = datetime.now(timezone.utc)
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id,
            status=SubtitleJobStatus.queued,
            request_json={"resume": True, RECOVERY_RELAUNCH_PENDING_KEY: True},
            updated_at=now,
        )
        db.add(job)
        db.flush()
        job_id = job.id
        recent = PipelineRun(
            task_id=task.id,
            engine="hatchet",
            workflow_name=SUBTITLE_JOB_WORKFLOW_NAME,
            external_run_id="recent-run",
            state="submitted",
            request_json={"job_id": str(job.id), "priority": 0},
            created_at=now + timedelta(seconds=1),
            updated_at=now + timedelta(seconds=1),
        )
        db.add(recent)
        db.commit()

    class FailIfLaunched:
        def __init__(self, _settings: object) -> None:
            pass

        def launch_subtitle_job(self, _job_id, **_kwargs):
            raise AssertionError("a recent recovery run must not be duplicated")

    monkeypatch.setattr(scheduler_module, "get_sessionmaker", lambda _url: factory)
    monkeypatch.setattr(scheduler_module, "PipelineLauncher", FailIfLaunched)
    scheduler = scheduler_module.OrchestratorScheduler(SimpleNamespace(database_url="sqlite://"))

    assert scheduler._dispatch_recovered_subtitle_jobs_once() == 0

    with factory() as db:
        persisted_job = db.get(SubtitleJob, job_id)
        assert persisted_job is not None
        assert RECOVERY_RELAUNCH_PENDING_KEY not in persisted_job.request_json

    engine.dispose()


def test_recovery_scans_past_unmarked_queue_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory = _database()
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        db.add_all([
            SubtitleJob(task_id=task.id, status=SubtitleJobStatus.queued, request_json={})
            for _ in range(410)
        ])
        db.commit()
        marked = SubtitleJob(
            task_id=task.id, status=SubtitleJobStatus.queued,
            request_json={RECOVERY_RELAUNCH_PENDING_KEY: True},
        )
        db.add(marked)
        db.commit()
        marked_id = marked.id

    launched = []

    class Launcher:
        def __init__(self, _settings):
            pass

        def launch_subtitle_job(self, job_id, **_kwargs):
            launched.append(job_id)

    monkeypatch.setattr(scheduler_module, "get_sessionmaker", lambda _url: factory)
    monkeypatch.setattr(scheduler_module, "PipelineLauncher", Launcher)
    scheduler = scheduler_module.OrchestratorScheduler(SimpleNamespace(database_url="sqlite://"))
    assert scheduler._dispatch_recovered_subtitle_jobs_once() == 1
    assert launched == [marked_id]
    engine.dispose()


def test_reentrant_recovery_respects_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory = _database()
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id, status=SubtitleJobStatus.queued,
            request_json={RECOVERY_RELAUNCH_PENDING_KEY: True},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    scheduler = scheduler_module.OrchestratorScheduler(SimpleNamespace(database_url="sqlite://"))
    launched = []
    nested = []

    class Launcher:
        def __init__(self, _settings):
            pass

        def launch_subtitle_job(self, current_id, **_kwargs):
            launched.append(current_id)
            nested.append(scheduler._dispatch_recovered_subtitle_jobs_once())

    monkeypatch.setattr(scheduler_module, "get_sessionmaker", lambda _url: factory)
    monkeypatch.setattr(scheduler_module, "PipelineLauncher", Launcher)
    assert scheduler._dispatch_recovered_subtitle_jobs_once() == 1
    assert launched == [job_id]
    assert nested == [0]
    engine.dispose()


def test_unconfirmed_submitting_does_not_clear_recovery_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = _database()
    now = datetime.now(timezone.utc)
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id, status=SubtitleJobStatus.queued,
            request_json={RECOVERY_RELAUNCH_PENDING_KEY: True}, updated_at=now,
        )
        db.add(job)
        db.flush()
        job_id = job.id
        db.add(PipelineRun(
            task_id=task.id, engine="hatchet",
            workflow_name=SUBTITLE_JOB_WORKFLOW_NAME, state="submitting",
            created_at=now + timedelta(seconds=1),
            request_json={"job_id": str(job.id)},
        ))
        db.commit()

    class Launcher:
        def __init__(self, _settings):
            pass

        def launch_subtitle_job(self, _job_id, **_kwargs):
            raise AssertionError("in-flight submission must be deferred")

    monkeypatch.setattr(scheduler_module, "get_sessionmaker", lambda _url: factory)
    monkeypatch.setattr(scheduler_module, "PipelineLauncher", Launcher)
    scheduler = scheduler_module.OrchestratorScheduler(SimpleNamespace(database_url="sqlite://"))
    assert scheduler._dispatch_recovered_subtitle_jobs_once() == 0
    with factory() as db:
        assert db.get(SubtitleJob, job_id).request_json[RECOVERY_RELAUNCH_PENDING_KEY] is True
    engine.dispose()


def test_failed_launch_keeps_recovery_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory = _database()
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id, status=SubtitleJobStatus.queued,
            request_json={RECOVERY_RELAUNCH_PENDING_KEY: True},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    calls = []

    class Launcher:
        def __init__(self, _settings):
            pass

        def launch_subtitle_job(self, current_id, **_kwargs):
            calls.append(current_id)
            raise RuntimeError("Hatchet unavailable")

    monkeypatch.setattr(scheduler_module, "get_sessionmaker", lambda _url: factory)
    monkeypatch.setattr(scheduler_module, "PipelineLauncher", Launcher)
    scheduler = scheduler_module.OrchestratorScheduler(SimpleNamespace(database_url="sqlite://"))
    assert scheduler._dispatch_recovered_subtitle_jobs_once() == 0
    assert scheduler._dispatch_recovered_subtitle_jobs_once() == 0
    assert calls == [job_id]
    with factory() as db:
        assert db.get(SubtitleJob, job_id).request_json[RECOVERY_RELAUNCH_PENDING_KEY] is True
    engine.dispose()
