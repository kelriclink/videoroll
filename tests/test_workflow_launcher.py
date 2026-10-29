from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.db.base import Base
from videoroll.db.models import OutboxEvent, PipelineRun, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.workflows.launcher import PipelineLauncher
from videoroll.workflows.settings import WorkflowRuntimeSettings


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return dict(self.payload)


class _Client:
    def __init__(self, *, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {"workflow_run_id": "hatchet-123"}
        self.error = error
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, *, json: dict[str, Any]) -> _Response:
        self.posts.append((url, dict(json)))
        if self.error is not None:
            raise self.error
        return _Response(self.payload)


def _database() -> tuple[object, sessionmaker[Session], uuid.UUID]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__, PipelineRun.__table__, OutboxEvent.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own, priority=100)
        db.add(task)
        db.commit()
        task_id = task.id
    return engine, factory, task_id


def _settings() -> Any:
    return SimpleNamespace(
        database_url="sqlite://",
        workflow_service_url="http://workflow-api:8030",
        internal_api_secret="unit-test-internal-secret",
        development_mode=False,
    )


def test_pipeline_launcher_records_hatchet_run_without_scheduler_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module

    engine, factory, task_id = _database()
    client = _Client()
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)
    launcher = PipelineLauncher(_settings(), session_factory=factory)

    result = launcher.launch_auto_youtube(task_id)

    assert result.workflow_name == "VideoPipelineV1"
    assert result.external_run_id == "hatchet-123"
    assert client.posts == [
        ("http://workflow-api:8030/runs/auto-youtube", {
            "task_id": str(task_id), "priority": 100,
            "dispatch_key": f"auto-youtube:{result.pipeline_run_id}",
        })
    ]
    with factory() as db:
        row = db.get(PipelineRun, result.pipeline_run_id)
        assert row is not None
        assert row.task_id == task_id
        assert row.engine == "hatchet"
        assert row.state == "submitted"
        assert row.external_run_id == "hatchet-123"
        assert row.request_json == {"task_id": str(task_id), "runtime_profile": True,
                                    "priority": 100, "dispatch_key": f"auto-youtube:{result.pipeline_run_id}"}
    assert {column.name for column in PipelineRun.__table__.columns}.isdisjoint(
        {"lease_owner", "lease_until", "heartbeat_at", "worker_id"}
    )
    engine.dispose()


def test_pipeline_launcher_submits_subtitle_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module

    engine, factory, task_id = _database()
    with factory() as db:
        job = SubtitleJob(
            task_id=task_id,
            request_json={"task_id": str(task_id)},
            status=SubtitleJobStatus.queued,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    client = _Client(payload={"workflow_run_id": "subtitle-run-1"})
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)
    launcher = PipelineLauncher(_settings(), session_factory=factory)

    result = launcher.launch_subtitle_job(job_id)

    assert result.workflow_name == "SubtitleJobV1"
    assert result.external_run_id == "subtitle-run-1"
    assert client.posts == [
        ("http://workflow-api:8030/runs/subtitle-job", {
            "job_id": str(job_id), "priority": 100,
            "dispatch_key": f"subtitle:{result.pipeline_run_id}",
        })
    ]
    with factory() as db:
        row = db.get(PipelineRun, result.pipeline_run_id)
        assert row is not None
        assert row.task_id == task_id
        assert row.workflow_name == "SubtitleJobV1"
        assert row.request_json == {"job_id": str(job_id), "priority": 100,
                                    "dispatch_key": f"subtitle:{result.pipeline_run_id}"}
    engine.dispose()


def test_recovery_launcher_passes_stable_dispatch_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module

    engine, factory, task_id = _database()
    with factory() as db:
        job = SubtitleJob(
            task_id=task_id, request_json={}, status=SubtitleJobStatus.queued,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    client = _Client()
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)
    launcher = PipelineLauncher(_settings(), session_factory=factory)
    result = launcher.launch_subtitle_job(job_id, dispatch_key="recover-job-unique-key")
    assert result.external_run_id == "hatchet-123"
    assert client.posts == [
        ("http://workflow-api:8030/runs/subtitle-job", {
            "job_id": str(job_id),
            "priority": 100,
            "dispatch_key": "recover-job-unique-key",
        })
    ]
    with factory() as db:
        assert db.get(PipelineRun, result.pipeline_run_id).request_json["dispatch_key"] == "recover-job-unique-key"
    engine.dispose()


def test_subtitle_recovery_reuses_launch_failed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module

    engine, factory, task_id = _database()
    with factory() as db:
        job = SubtitleJob(
            task_id=task_id,
            request_json={},
            status=SubtitleJobStatus.queued,
        )
        db.add(job)
        db.flush()
        job_id = job.id
        run = PipelineRun(
            task_id=task_id,
            engine="hatchet",
            workflow_name="SubtitleJobV1",
            state="launch_failed",
            request_json={
                "job_id": str(job_id),
                "priority": 100,
                "dispatch_key": "stable-recovery-key",
            },
            error_message="ConnectError: connection refused",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    client = _Client(payload={"workflow_run_id": "subtitle-recovered"})
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)

    result = PipelineLauncher(_settings(), session_factory=factory).launch_subtitle_job(
        job_id,
        dispatch_key="stable-recovery-key",
    )

    assert result.pipeline_run_id == run_id
    assert result.external_run_id == "subtitle-recovered"
    with factory() as db:
        rows = db.query(PipelineRun).filter(PipelineRun.task_id == task_id).all()
        assert len(rows) == 1
        assert rows[0].state == "submitted"
        assert rows[0].error_message is None


def test_pipeline_launcher_persists_workflow_service_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module

    engine, factory, task_id = _database()
    client = _Client(error=RuntimeError("workflow service unavailable"))
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)
    launcher = PipelineLauncher(_settings(), session_factory=factory)

    with pytest.raises(RuntimeError, match="workflow service unavailable"):
        launcher.launch_auto_youtube(task_id)

    with factory() as db:
        row = db.query(PipelineRun).one()
        assert row.engine == "hatchet"
        assert row.state == "launch_failed"
        assert row.finished_at is not None
        assert "workflow service unavailable" in str(row.error_message)
    engine.dispose()


def test_workflow_runtime_settings_has_no_execution_engine_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_ENGINE", "celery")
    monkeypatch.setenv("DEVELOPMENT_MODE", "true")
    monkeypatch.delenv("HATCHET_WORKER_NAME", raising=False)
    monkeypatch.delenv("HATCHET_WORKER_SLOTS", raising=False)

    settings = WorkflowRuntimeSettings.from_env()

    assert not hasattr(settings, "engine")
    assert settings.worker_slots == 4
    assert settings.orchestrator_url == "http://orchestrator:8000"


def test_submit_then_stop_before_hatchet_response_enqueues_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module
    from videoroll.apps.orchestrator_api.services import task_service

    engine, factory, task_id = _database()
    client = _Client()
    original_post = client.post

    def stop_during_post(url: str, *, json: dict[str, Any]):
        with factory() as db:
            task_service.stop_task(task_id, db=db)
            rows = db.query(PipelineRun).all()
            assert len(rows) == 1
            assert rows[0].state == "cancel_requested"
            assert rows[0].external_run_id is None
            assert db.query(OutboxEvent).count() == 0
        return original_post(url, json=json)

    monkeypatch.setattr(client, "post", stop_during_post)
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)
    result = PipelineLauncher(_settings(), session_factory=factory).launch_auto_youtube(task_id)
    with factory() as db:
        row = db.get(PipelineRun, result.pipeline_run_id)
        assert row.state == "cancel_requested"
        event = db.query(OutboxEvent).one()
        assert event.args_json["args"] == [str(row.id), "hatchet-123"]
    engine.dispose()


def test_lost_auto_youtube_response_reuses_same_dispatch_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module

    engine, factory, task_id = _database()
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: _Client(error=TimeoutError("lost response")))
    launcher = PipelineLauncher(_settings(), session_factory=factory)
    with pytest.raises(TimeoutError):
        launcher.launch_auto_youtube(task_id)
    with factory() as db:
        row = db.query(PipelineRun).one()
        first_id, key = row.id, row.request_json["dispatch_key"]
        assert row.state == "launch_failed"

    client = _Client()
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)
    result = launcher.launch_auto_youtube(task_id)
    assert result.pipeline_run_id == first_id
    assert client.posts[0][1]["dispatch_key"] == key
    with factory() as db:
        assert db.query(PipelineRun).count() == 1
        assert db.query(PipelineRun).one().state == "submitted"
    engine.dispose()


def test_canceled_lost_hatchet_response_is_reconciled_and_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.workflows import launcher as launcher_module
    from videoroll.apps.orchestrator_api.services import task_service

    engine, factory, task_id = _database()
    launcher = PipelineLauncher(_settings(), session_factory=factory)
    failure = _Client(error=TimeoutError("response lost after acceptance"))
    original_post = failure.post

    def stop_then_timeout(url: str, *, json: dict[str, Any]):
        with factory() as db:
            task_service.stop_task(task_id, db=db)
        return original_post(url, json=json)

    monkeypatch.setattr(failure, "post", stop_then_timeout)
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: failure)
    with pytest.raises(TimeoutError):
        launcher.launch_auto_youtube(task_id)
    with factory() as db:
        row = db.query(PipelineRun).one()
        assert row.state == "cancel_requested"
        assert row.external_run_id is None
        key = row.request_json["dispatch_key"]

    client = _Client()
    monkeypatch.setattr(launcher_module.httpx, "Client", lambda *args, **kwargs: client)
    assert launcher.reconcile_unresolved_cancellations() == 1
    assert client.posts[0][1]["dispatch_key"] == key
    with factory() as db:
        row = db.query(PipelineRun).one()
        assert row.state == "cancel_requested"
        assert row.external_run_id == "hatchet-123"
        assert db.query(OutboxEvent).one().args_json["args"] == [str(row.id), "hatchet-123"]
    assert launcher.reconcile_unresolved_cancellations() == 0
    engine.dispose()


def test_launch_response_does_not_regress_a_completed_callback_to_submitted() -> None:
    engine, factory, task_id = _database()
    with factory() as db:
        run = PipelineRun(task_id=task_id, engine='hatchet', workflow_name='VideoPipelineV1',
                          state='succeeded', request_json={'dispatch_key': 'already-done'})
        db.add(run)
        db.commit()
        run_id = run.id
    with factory() as db:
        PipelineLauncher._finish_external_launch(db, run_id, 'hatchet-fast-completed')
    with factory() as db:
        assert db.get(PipelineRun, run_id).state == 'succeeded'
        assert db.get(PipelineRun, run_id).external_run_id == 'hatchet-fast-completed'
    engine.dispose()
