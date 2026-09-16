from __future__ import annotations

import uuid
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.schemas import RemoteJobResponse, SubtitleActionRequest
from videoroll.apps.orchestrator_api.services import subtitle_service
from videoroll.apps.subtitle_service import main as subtitle_api
from videoroll.apps.subtitle_service.schemas import SubtitleJobCreate
from videoroll.config import OrchestratorSettings
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, Asset.__table__, SubtitleJob.__table__])
    monkeypatch.setattr("videoroll.realtime.publish_ui_event", lambda *args, **kwargs: True)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        _env_file=None,
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://127.0.0.1:1/0",
        INTERNAL_API_SECRET="subtitle-admission-test-secret",
        ADMIN_BOOTSTRAP_SECRET="subtitle-admission-bootstrap-secret",
    )


def _task(db: Session, status: TaskStatus) -> Task:
    task = Task(source_type=SourceType.local, source_license=SourceLicense.own, status=status)
    db.add(task)
    db.flush()
    db.add(Asset(task_id=task.id, kind=AssetKind.video_raw, storage_key=f"raw/{task.id}/source.mp4"))
    db.commit()
    return task


@pytest.mark.parametrize("resume", [False, True])
def test_orchestrator_rejects_published_subtitle_actions_before_forwarding(db: Session, monkeypatch, resume: bool) -> None:
    task = _task(db, TaskStatus.published)
    db.add(SubtitleJob(task_id=task.id, status=SubtitleJobStatus.failed, request_json={"input": {"key": "raw.mp4"}}))
    db.commit()
    forwarded: list[dict] = []

    def forward(_settings, request):
        forwarded.append(request)
        return RemoteJobResponse(job_id=uuid.uuid4(), status="queued")

    monkeypatch.setattr(subtitle_service, "enqueue_subtitle_service_job_request", forward)
    with pytest.raises(HTTPException) as caught:
        if resume:
            subtitle_service.resume_subtitle_job(task.id, settings=_settings(), db=db)
        else:
            subtitle_service.enqueue_subtitle_job(task.id, SubtitleActionRequest(), settings=_settings(), db=db, s3=object())

    assert caught.value.status_code == 409
    assert "published" in str(caught.value.detail)
    assert forwarded == []


@pytest.mark.parametrize("status", [TaskStatus.published, TaskStatus.canceled])
def test_internal_subtitle_api_does_not_create_jobs_for_terminal_tasks(db: Session, monkeypatch, status: TaskStatus) -> None:
    task = _task(db, status)
    dispatched: list[str] = []
    monkeypatch.setattr(subtitle_api.celery_app, "send_task", lambda name, **kwargs: dispatched.append(name))
    request = SubtitleJobCreate(task_id=task.id, input={"key": f"raw/{task.id}/source.mp4"})

    with pytest.raises(HTTPException) as caught:
        subtitle_api.create_job(request, db)

    assert caught.value.status_code == 409
    assert db.query(SubtitleJob).count() == 0
    assert dispatched == []


def test_internal_subtitle_api_accepts_an_unfinished_task(db: Session, monkeypatch) -> None:
    task = _task(db, TaskStatus.downloaded)
    monkeypatch.setattr(subtitle_api.celery_app, "send_task", lambda *args, **kwargs: None)

    response = subtitle_api.create_job(SubtitleJobCreate(task_id=task.id, input={"key": "raw/source.mp4"}), db)

    job = db.get(SubtitleJob, uuid.UUID(response["job_id"]))
    assert response["status"] == "queued"
    assert job is not None and job.task_id == task.id


def test_subtitle_relay_preserves_a_conflict_detected_by_the_internal_api() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={"detail": "task is already published"}))
    client = httpx.Client(transport=transport)
    with patch.object(subtitle_service.httpx, "Client", return_value=client):
        with pytest.raises(HTTPException) as caught:
            subtitle_service.enqueue_subtitle_service_job_request(_settings(), {"task_id": str(uuid.uuid4())})

    assert caught.value.status_code == 409
    assert caught.value.detail == "task is already published"
