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

from videoroll.apps.orchestrator_api.schemas import (
    RemoteJobResponse,
    SubtitleActionRequest,
    SubtitleRetranslateRequest,
)
from videoroll.apps.orchestrator_api.services import subtitle_service
from videoroll.apps.subtitle_service import main as subtitle_api
from videoroll.apps.subtitle_service.schemas import SubtitleJobCreate
from videoroll.config import OrchestratorSettings
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.utils.auto_youtube import encode_auto_youtube_created_by


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
            subtitle_service.enqueue_subtitle_job(task.id, SubtitleActionRequest(), settings=_settings(), db=db, store=object())

    assert caught.value.status_code == 409
    assert "published" in str(caught.value.detail)
    assert forwarded == []


@pytest.mark.parametrize("status", [TaskStatus.published, TaskStatus.canceled])
def test_internal_subtitle_api_does_not_create_jobs_for_terminal_tasks(db: Session, status: TaskStatus) -> None:
    task = _task(db, status)
    request = SubtitleJobCreate(task_id=task.id, input={"key": f"raw/{task.id}/source.mp4"})

    with pytest.raises(HTTPException) as caught:
        subtitle_api.create_job(request, db)

    assert caught.value.status_code == 409
    assert db.query(SubtitleJob).count() == 0


def test_internal_subtitle_api_accepts_an_unfinished_task(db: Session) -> None:
    task = _task(db, TaskStatus.downloaded)

    response = subtitle_api.create_job(SubtitleJobCreate(task_id=task.id, input={"key": "raw/source.mp4"}), db)

    job = db.get(SubtitleJob, uuid.UUID(response["job_id"]))
    assert response["status"] == "queued"
    assert job is not None and job.task_id == task.id


def test_automatic_job_infers_runtime_profile_but_explicit_manual_request_does_not(db: Session) -> None:
    task = _task(db, TaskStatus.downloaded)
    task.created_by = encode_auto_youtube_created_by("auto_youtube", auto_publish=None)
    db.add(task)
    db.commit()

    legacy = subtitle_api.create_job(SubtitleJobCreate(task_id=task.id, input={"key": "raw/source.mp4"}), db)
    legacy_job = db.get(SubtitleJob, uuid.UUID(legacy["job_id"]))
    assert legacy_job is not None
    assert legacy_job.request_json["runtime_profile"] is True

    legacy_job.status = SubtitleJobStatus.failed
    db.add(legacy_job)
    db.commit()
    manual_request = subtitle_service.build_subtitle_job_request(
        task.id,
        SubtitleActionRequest(asr_engine="openvino", video_codec="h264"),
        db.query(Asset).filter(Asset.task_id == task.id, Asset.kind == AssetKind.video_raw).one(),
    )
    assert manual_request["runtime_profile"] is False
    manual = subtitle_api.create_job(SubtitleJobCreate.model_validate(manual_request), db)
    manual_job = db.get(SubtitleJob, uuid.UUID(manual["job_id"]))
    assert manual_job is not None
    assert manual_job.request_json["runtime_profile"] is False
    assert manual_job.request_json["asr"]["engine"] == "openvino"


def test_selective_retranslation_reuses_aligned_snapshot_and_context(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(db, TaskStatus.subtitle_ready)
    previous_request = {
        "task_id": str(task.id),
        "resume": False,
        "runtime_profile": False,
        "input": {"type": "storage", "key": f"raw/{task.id}/source.mp4"},
        "asr": {"engine": "faster-whisper", "language": "en", "model": None},
        "translate": {
            "enabled": True,
            "provider": "openai",
            "target_lang": "zh",
            "style": "自然",
            "bilingual": False,
        },
        "output": {"formats": ["srt", "ass"], "render": {"burn_in": False}},
        "output_prefix": f"sub/{task.id}/",
        "artifacts": {
            "translated_segments_key": f"sub/{task.id}/translated.json",
            "translation_context_key": f"sub/{task.id}/context.json",
            "subtitle_quality_key": f"sub/{task.id}/quality.json",
        },
    }
    previous = SubtitleJob(
        task_id=task.id,
        status=SubtitleJobStatus.succeeded,
        request_json=previous_request,
    )
    db.add(previous)
    db.commit()

    forwarded: list[dict] = []

    def forward(_settings, request):
        forwarded.append(request)
        return RemoteJobResponse(job_id=uuid.uuid4(), status="queued")

    monkeypatch.setattr(subtitle_service, "enqueue_subtitle_service_job_request", forward)

    result = subtitle_service.enqueue_selective_subtitle_retranslation(
        task.id,
        SubtitleRetranslateRequest(indices=[5, 2, 5, 0, -1]),
        settings=_settings(),
        db=db,
    )

    assert result.status == "queued"
    assert len(forwarded) == 1
    request = forwarded[0]
    assert request["resume"] is True
    assert request["asr"] == previous_request["asr"]
    assert request["translate"] == previous_request["translate"]
    assert request["output"] == previous_request["output"]
    assert request["selective_retranslate"] == {
        "indices": [2, 5],
        "base_translated_segments_key": f"sub/{task.id}/translated.json",
        "translation_context_key": f"sub/{task.id}/context.json",
    }


def test_selective_retranslation_requires_aligned_translation_snapshot(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(db, TaskStatus.subtitle_ready)
    db.add(
        SubtitleJob(
            task_id=task.id,
            status=SubtitleJobStatus.succeeded,
            request_json={
                "task_id": str(task.id),
                "input": {"type": "storage", "key": f"raw/{task.id}/source.mp4"},
                "translate": {"enabled": True, "provider": "openai"},
                "artifacts": {"translation_context_key": f"sub/{task.id}/context.json"},
            },
        )
    )
    db.commit()
    forwarded: list[dict] = []
    monkeypatch.setattr(
        subtitle_service,
        "enqueue_subtitle_service_job_request",
        lambda _settings, request: forwarded.append(request),
    )

    with pytest.raises(HTTPException) as caught:
        subtitle_service.enqueue_selective_subtitle_retranslation(
            task.id,
            SubtitleRetranslateRequest(indices=[1]),
            settings=_settings(),
            db=db,
        )

    assert caught.value.status_code == 409
    assert "aligned translation snapshots" in str(caught.value.detail)
    assert forwarded == []


def test_selective_retranslation_rejects_published_task(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(db, TaskStatus.published)
    forwarded: list[dict] = []
    monkeypatch.setattr(
        subtitle_service,
        "enqueue_subtitle_service_job_request",
        lambda _settings, request: forwarded.append(request),
    )

    with pytest.raises(HTTPException) as caught:
        subtitle_service.enqueue_selective_subtitle_retranslation(
            task.id,
            SubtitleRetranslateRequest(indices=[1]),
            settings=_settings(),
            db=db,
        )

    assert caught.value.status_code == 409
    assert "published" in str(caught.value.detail)
    assert forwarded == []


def test_subtitle_relay_preserves_a_conflict_detected_by_the_internal_api() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={"detail": "task is already published"}))
    client = httpx.Client(transport=transport)
    with patch.object(subtitle_service.httpx, "Client", return_value=client):
        with pytest.raises(HTTPException) as caught:
            subtitle_service.enqueue_subtitle_service_job_request(_settings(), {"task_id": str(uuid.uuid4())})

    assert caught.value.status_code == 409
    assert caught.value.detail == "task is already published"
