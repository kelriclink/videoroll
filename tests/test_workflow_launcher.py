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
from videoroll.db.models import PipelineRun, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task
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
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__, PipelineRun.__table__])
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
        ("http://workflow-api:8030/runs/auto-youtube", {"task_id": str(task_id), "priority": 100})
    ]
    with factory() as db:
        row = db.get(PipelineRun, result.pipeline_run_id)
        assert row is not None
        assert row.task_id == task_id
        assert row.engine == "hatchet"
        assert row.state == "submitted"
        assert row.external_run_id == "hatchet-123"
        assert row.request_json == {"task_id": str(task_id), "runtime_profile": True, "priority": 100}
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
        ("http://workflow-api:8030/runs/subtitle-job", {"job_id": str(job_id), "priority": 100})
    ]
    with factory() as db:
        row = db.get(PipelineRun, result.pipeline_run_id)
        assert row is not None
        assert row.task_id == task_id
        assert row.workflow_name == "SubtitleJobV1"
        assert row.request_json == {"job_id": str(job_id), "priority": 100}
    engine.dispose()


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
