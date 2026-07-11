from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.apps.orchestrator_api.schemas import (
    RecentFailedResumeItem,
    RecentFailedResumeResponse,
    RemoteJobResponse,
    SubtitleActionRequest,
)
from videoroll.apps.orchestrator_api.services import publishing_service, youtube_service
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.config import OrchestratorSettings
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
from videoroll.storage.s3 import S3Store
from videoroll.utils.auto_youtube import encode_auto_youtube_created_by


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def enqueue_subtitle_service_job_request(
    settings: OrchestratorSettings,
    request: dict[str, Any],
) -> RemoteJobResponse:
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            response = client.post(f"{settings.subtitle_service_url}/subtitle/jobs", json=request)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"subtitle-service request failed: {exc}") from exc
    return RemoteJobResponse(job_id=uuid.UUID(data["job_id"]), status=str(data.get("status", "queued")))


def build_resume_subtitle_request(
    task_id: uuid.UUID,
    db: Session,
    *,
    after_render: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = (
        db.query(SubtitleJob)
        .filter(SubtitleJob.task_id == task_id)
        .order_by(SubtitleJob.created_at.desc())
        .first()
    )
    if not previous:
        raise HTTPException(status_code=400, detail="no subtitle job found to resume")
    request_in = previous.request_json if isinstance(previous.request_json, dict) else {}
    if not request_in:
        raise HTTPException(status_code=400, detail="subtitle job request is empty")
    request_out = dict(request_in)
    request_out["task_id"] = str(task_id)
    request_out["resume"] = True
    if not isinstance(request_out.get("output_prefix"), str) or not str(request_out.get("output_prefix") or "").strip():
        request_out["output_prefix"] = f"sub/{task_id}/"
    if after_render is not None:
        request_out["after_render"] = after_render
    return request_out


def build_subtitle_job_request(
    task_id: uuid.UUID,
    payload: SubtitleActionRequest,
    raw_asset: Asset,
) -> dict[str, Any]:
    youtube_subtitle_mode = payload.youtube_subtitle_mode
    if "youtube_subtitle_mode" not in payload.model_fields_set:
        youtube_subtitle_mode = "target" if payload.prefer_youtube_subtitles else "off"
    translate: dict[str, Any] = {
        "enabled": payload.translate_enabled,
        "target_lang": payload.target_lang,
        "provider": payload.translate_provider,
        "style": payload.translate_style,
        "glossary_id": None,
        "bilingual": payload.bilingual,
    }
    if payload.translate_batch_size is not None:
        translate["batch_size"] = payload.translate_batch_size
    if payload.translate_enable_summary is not None:
        translate["enable_summary"] = payload.translate_enable_summary
    return {
        "task_id": str(task_id),
        "resume": bool(payload.resume),
        "prefer_youtube_subtitles": youtube_subtitle_mode != "off",
        "youtube_subtitle_mode": youtube_subtitle_mode,
        "input": {"type": "s3", "key": raw_asset.storage_key},
        "asr": {"engine": payload.asr_engine, "language": payload.asr_language, "model": payload.asr_model},
        "translate": translate,
        "output": {
            "formats": payload.formats,
            "render": {
                "burn_in": payload.burn_in,
                "soft_sub": payload.soft_sub,
                "ass_style": payload.ass_style,
                "video_codec": payload.video_codec,
                "use_intel_gpu": payload.use_intel_gpu,
                "video_preset": payload.video_preset,
                "video_crf": payload.video_crf,
            },
        },
        "output_prefix": f"sub/{task_id}/",
    }


def list_task_subtitle_jobs(task_id: uuid.UUID, *, limit: int, db: Session) -> list[SubtitleJob]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return (
        db.query(SubtitleJob)
        .filter(SubtitleJob.task_id == task_id)
        .order_by(SubtitleJob.created_at.desc())
        .limit(limit)
        .all()
    )


def enqueue_subtitle_job(
    task_id: uuid.UUID,
    payload: SubtitleActionRequest,
    *,
    settings: OrchestratorSettings,
    db: Session,
    s3: S3Store,
) -> RemoteJobResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    in_flight = (
        db.query(SubtitleJob)
        .filter(
            SubtitleJob.task_id == task_id,
            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
        )
        .order_by(SubtitleJob.created_at.desc())
        .first()
    )
    if in_flight:
        try:
            with httpx.Client(timeout=5.0, headers=internal_http_headers(settings)) as client:
                client.post(f"{settings.subtitle_service_url}/subtitle/task_queue/tick")
        except httpx.HTTPError:
            pass
        return RemoteJobResponse(job_id=in_flight.id, status=in_flight.status.value)
    raw_asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_raw)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not raw_asset:
        message = "no raw video asset found; upload video first"
        if task.source_type.value == "youtube" and str(task.source_url or "").strip():
            message = f"{message} (youtube task: POST /tasks/{task_id}/actions/youtube_download)"
        raise HTTPException(status_code=400, detail=message)
    if payload.burn_in and payload.use_intel_gpu and str(payload.video_codec or "").strip().lower() not in {
        "h264",
        "avc",
        "av1",
    }:
        raise HTTPException(status_code=400, detail="Intel GPU burn-in currently requires video_codec=h264 or av1")
    request = build_subtitle_job_request(task_id, payload, raw_asset)
    if payload.auto_publish:
        request["after_render"] = publishing_service.build_auto_publish_after_render(
            task,
            db=db,
            s3=s3,
            publish_payload_overrides=dict(payload.publish_payload or {}),
        )
    return enqueue_subtitle_service_job_request(settings, request)


def resume_subtitle_job(
    task_id: uuid.UUID,
    *,
    settings: OrchestratorSettings,
    db: Session,
) -> RemoteJobResponse:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    in_flight = (
        db.query(SubtitleJob)
        .filter(
            SubtitleJob.task_id == task_id,
            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
        )
        .count()
    )
    if in_flight:
        raise HTTPException(status_code=409, detail="subtitle job already in progress")
    return enqueue_subtitle_service_job_request(settings, build_resume_subtitle_request(task_id, db))


def resume_recent_failed_tasks(
    *,
    window_hours: int,
    limit: int,
    settings: OrchestratorSettings,
    db: Session,
    s3: S3Store,
) -> RecentFailedResumeResponse:
    cutoff = utcnow() - timedelta(hours=window_hours)
    tasks = (
        db.query(Task)
        .filter(Task.status == TaskStatus.failed, Task.updated_at >= cutoff)
        .order_by(Task.updated_at.desc(), Task.created_at.desc())
        .limit(limit)
        .all()
    )
    resumed_count = 0
    skipped_count = 0
    failed_count = 0
    results: list[RecentFailedResumeItem] = []
    for task in tasks:
        subtitle_inflight = (
            db.query(SubtitleJob)
            .filter(
                SubtitleJob.task_id == task.id,
                SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
            )
            .count()
        )
        render_inflight = (
            db.query(RenderJob)
            .filter(
                RenderJob.task_id == task.id,
                RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
            )
            .count()
        )
        if subtitle_inflight or render_inflight:
            skipped_count += 1
            results.append(
                RecentFailedResumeItem(
                    task_id=task.id,
                    status="skipped",
                    detail="subtitle/render job already in progress for this task",
                )
            )
            continue
        if task.source_license == SourceLicense.unknown:
            skipped_count += 1
            results.append(
                RecentFailedResumeItem(
                    task_id=task.id,
                    status="skipped",
                    detail="source_license=unknown; add proof before auto publish",
                )
            )
            continue
        try:
            after_render = publishing_service.build_auto_publish_after_render(task, db=db, s3=s3)
            request = build_resume_subtitle_request(task.id, db, after_render=after_render)
            remote = enqueue_subtitle_service_job_request(settings, request)
            resumed_count += 1
            results.append(RecentFailedResumeItem(task_id=task.id, job_id=remote.job_id, status=remote.status))
        except HTTPException as exc:
            detail = str(exc.detail) if exc.detail is not None else str(exc)
            if (
                exc.status_code == 400
                and detail == "no subtitle job found to resume"
                and task.source_type == SourceType.youtube
                and str(task.source_url or "").strip()
            ):
                auto_publish = bool(get_auto_profile(db).get("auto_publish"))
                youtube_service.set_task_created_by(
                    settings,
                    task_id=task.id,
                    created_by=encode_auto_youtube_created_by("youtube_task_restart", auto_publish=auto_publish),
                )
                pipeline_job_id = youtube_service.enqueue_auto_youtube_pipeline(
                    task.id,
                    auto_publish=auto_publish,
                )
                resumed_count += 1
                results.append(
                    RecentFailedResumeItem(
                        task_id=task.id,
                        status="queued",
                        detail=f"started auto_youtube pipeline: {pipeline_job_id}",
                    )
                )
                continue
            if exc.status_code in {400, 404, 409}:
                skipped_count += 1
                results.append(RecentFailedResumeItem(task_id=task.id, status="skipped", detail=detail))
                continue
            failed_count += 1
            results.append(RecentFailedResumeItem(task_id=task.id, status="error", detail=detail))
    return RecentFailedResumeResponse(
        window_hours=window_hours,
        matched_count=len(tasks),
        resumed_count=resumed_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        results=results,
    )
