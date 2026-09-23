from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.schemas import RemoteJobResponse
from videoroll.apps.orchestrator_api.services import subtitle_service
from videoroll.db.base import Base
from videoroll.db.models import (
    Asset,
    AssetKind,
    RenderJob,
    RenderJobStatus,
    SourceLicense,
    SourceType,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def _database() -> tuple[object, sessionmaker[Session], uuid.UUID]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Task.__table__, Asset.__table__, SubtitleJob.__table__, RenderJob.__table__],
    )
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        task = Task(
            source_type=SourceType.youtube,
            source_license=SourceLicense.own,
            source_url="https://www.youtube.com/watch?v=test",
            status=TaskStatus.downloaded,
        )
        db.add(task)
        db.commit()
        task_id = task.id
    return engine, factory, task_id


def test_auto_subtitle_handoff_submits_runtime_profile_request(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory, task_id = _database()
    remote_job_id = uuid.uuid4()
    captured: dict[str, object] = {}

    with factory() as db:
        db.add(Asset(task_id=task_id, kind=AssetKind.video_raw, storage_key=f"raw/{task_id}/video.mp4"))
        db.commit()

        def fake_enqueue(
            _settings: object,
            request: dict[str, object],
            *,
            launch_workflow: bool = True,
        ) -> RemoteJobResponse:
            captured.update(request)
            captured["launch_workflow"] = launch_workflow
            return RemoteJobResponse(job_id=remote_job_id, status="queued")

        monkeypatch.setattr(subtitle_service, "enqueue_subtitle_service_job_request", fake_enqueue)
        response = subtitle_service.enqueue_auto_subtitle_handoff(
            task_id,
            settings=object(),  # type: ignore[arg-type]
            db=db,
        )

    assert response.status == "queued"
    assert response.job_id == remote_job_id
    assert response.job_kind == "subtitle"
    assert captured["runtime_profile"] is True
    assert captured["input"] == {"type": "storage", "key": f"raw/{task_id}/video.mp4"}
    assert captured["after_render"] == {"publish": True, "runtime_profile": True}
    assert captured["launch_workflow"] is False
    engine.dispose()


def test_auto_subtitle_handoff_reuses_active_subtitle_job(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory, task_id = _database()
    with factory() as db:
        job = SubtitleJob(
            task_id=task_id,
            request_json={"task_id": str(task_id)},
            status=SubtitleJobStatus.queued,
            progress=0,
        )
        db.add(job)
        db.commit()
        job_id = job.id
        monkeypatch.setattr(
            subtitle_service,
            "enqueue_subtitle_service_job_request",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue duplicate")),
        )

        settings = object()
        response = subtitle_service.enqueue_auto_subtitle_handoff(
            task_id,
            settings=settings,  # type: ignore[arg-type]
            db=db,
        )

    assert response.job_id == job_id
    assert response.job_kind == "subtitle"
    assert response.status == SubtitleJobStatus.queued.value
    engine.dispose()


def test_auto_subtitle_handoff_reuses_active_render_job(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory, task_id = _database()
    with factory() as db:
        job = RenderJob(
            task_id=task_id,
            request_json={"task_id": str(task_id)},
            status=RenderJobStatus.running,
            progress=25,
        )
        db.add(job)
        db.commit()
        job_id = job.id

        response = subtitle_service.enqueue_auto_subtitle_handoff(
            task_id,
            settings=object(),  # type: ignore[arg-type]
            db=db,
        )

    assert response.job_id == job_id
    assert response.job_kind == "render"
    assert response.status == RenderJobStatus.running.value
    engine.dispose()


def test_auto_subtitle_handoff_requires_downloaded_asset() -> None:
    engine, factory, task_id = _database()
    with factory() as db, pytest.raises(HTTPException) as exc_info:
        subtitle_service.enqueue_auto_subtitle_handoff(
            task_id,
            settings=object(),  # type: ignore[arg-type]
            db=db,
        )
    assert exc_info.value.status_code == 409
    assert "raw video asset" in str(exc_info.value.detail)
    engine.dispose()
