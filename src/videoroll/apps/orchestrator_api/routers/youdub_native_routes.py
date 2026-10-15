"""Native-only VideoRoll dubbing API.

YouDub owns TTS and all media stages. Previous CosyVoice/Qwen jobs are
read-only audit history; new requests and retries must be native VoxCPM2.
"""
from __future__ import annotations

import subprocess
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from videoroll.apps.dubbing import youdub_control
from videoroll.apps.dubbing.job import create_dubbing_job, lease_is_active
from videoroll.apps.dubbing.control import request_control
from videoroll.apps.orchestrator_api.dependencies import get_db, get_settings
from videoroll.apps.orchestrator_api.schemas import DubbingActionRequest, RemoteJobResponse
from videoroll.config import OrchestratorSettings
from videoroll.db.models import DubbingJob, Task, TaskStatus
from videoroll.workflows.launcher import PipelineLauncher

router = APIRouter()


def worker_state() -> dict[str, str | bool]:
    try:
        check = subprocess.run(
            ["systemctl", "is-active", "videoroll@dubbing-workflow-worker.service"],
            capture_output=True, text=True, timeout=2,
        )
        active = check.returncode == 0 and check.stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        active = False
    return {"state": "active" if active else "inactive", "available": active}


class EngineControl(BaseModel):
    enabled: bool


@router.get("/settings/dubbing")
def settings(db: Session = Depends(get_db)) -> dict:
    return {"native": youdub_control.status(db), "worker": worker_state()}


@router.get("/settings/dubbing/native")
def engine_state(db: Session = Depends(get_db)) -> dict:
    return youdub_control.status(db)


@router.put("/settings/dubbing/native")
def engine_control(payload: EngineControl, db: Session = Depends(get_db)) -> dict:
    try:
        return youdub_control.configure(db, enabled=payload.enabled)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/tasks/{task_id}/actions/auto_dubbing")
def auto_dubbing(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, str | None]:
    """Visible VideoPipelineV1 stage: schedule YouDub BEFORE publication.

    Its deterministic operation key and Hatchet dispatch key make retries
    idempotent. No Bilibili mutation occurs in this stage.
    """
    from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
    from videoroll.db.models import SubtitleJob, SubtitleJobStatus, Asset, AssetKind
    from videoroll.utils.auto_youtube import parse_auto_youtube_created_by

    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "source task not found")
    profile = get_auto_profile(db)
    if not (profile.get("auto_publish") and profile.get("auto_dub_p2")
            and "bilibili" in (profile.get("auto_publish_platforms") or [])):
        return {"status": "skipped", "detail": "automatic P2 is disabled",
                "dubbing_job_id": None}
    if parse_auto_youtube_created_by(task.created_by) is None:
        raise HTTPException(409, "automatic P2 requires an automatic YouTube task")
    subtitle = (db.query(SubtitleJob).filter(
        SubtitleJob.task_id == task_id,
        SubtitleJob.status == SubtitleJobStatus.succeeded,
    ).order_by(SubtitleJob.created_at.desc()).first())
    source = (db.query(Asset).filter(
        Asset.task_id == task_id, Asset.kind == AssetKind.video_raw,
    ).order_by(Asset.created_at.desc()).first())
    if subtitle is None or source is None:
        raise HTTPException(409, "automatic P2 needs Chinese subtitles and source video")
    ensure_ready(db)
    try:
        job = create_dubbing_job(
            db, task_id=task_id, subtitle_job_id=subtitle.id,
            subtitle_video_key=source.storage_key,
            operation_key=f"auto-p2:videopipeline:{task_id}",
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    request = dict(job.request_json or {})
    if "auto_p2_prefix" not in request:
        request["auto_p2_prefix"] = profile.get("auto_dub_p2_prefix") or "【中配】"
        request["auto_p1_prefix"] = profile.get("publish_title_prefix") or "【熟肉】"
        request["auto_p2_source"] = "video_pipeline"
        job.request_json = request
        db.commit()
    if job.status == "succeeded":
        return {"status": "succeeded", "detail": "existing YouDub result ready",
                "dubbing_job_id": str(job.id)}
    if job.status not in ("queued", "preparing", "synthesizing", "aligning", "mixing"):
        raise HTTPException(409, f"YouDub job {job.id} ended as {job.status}; resolve before publishing")
    if job.status == "queued":
        try:
            PipelineLauncher(settings).launch_dubbing_job(
                job.id, dispatch_key=f"video-pipeline-auto-p2:{job.id}")
        except Exception as exc:
            raise HTTPException(
                503, f"YouDub dispatch pending: {type(exc).__name__}: {exc}"
            ) from exc
    return {"status": "queued", "detail": "YouDub is scheduled in the main workflow",
            "dubbing_job_id": str(job.id)}


@router.get("/tasks/{task_id}/dubbing_jobs")
def dubbing_history(task_id: uuid.UUID, db: Session = Depends(get_db)) -> list[dict]:
    jobs = db.query(DubbingJob).filter(DubbingJob.task_id == task_id).order_by(
        DubbingJob.created_at.desc()).limit(50).all()
    return [
        {"id": str(j.id), "task_id": str(j.task_id),
         "subtitle_job_id": str(j.subtitle_job_id) if j.subtitle_job_id else None,
         "status": j.status, "progress": j.progress,
         "operation_key": j.operation_key, "request": j.request_json,
         "error_message": j.error_message, "output": j.output_json,
         "created_at": j.created_at.isoformat() if j.created_at else None}
        for j in jobs
    ]


def ensure_ready(db: Session) -> None:
    state = youdub_control.status(db)
    if not state["accepting_jobs"]:
        raise HTTPException(409, "YouDub disabled or model unavailable; enable it in settings")
    if worker_state()["state"] != "active":
        raise HTTPException(503, "Hatchet dubbing worker unavailable")


@router.post("/tasks/{task_id}/actions/dubbing", response_model=RemoteJobResponse)
def create(
    task_id: uuid.UUID, payload: DubbingActionRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    ensure_ready(db)
    try:
        job = create_dubbing_job(
            db, task_id=task_id, subtitle_job_id=payload.subtitle_job_id,
            subtitle_video_key=payload.subtitle_video_key,
            operation_key=payload.operation_key, voice="reference",
            model_path="youdub:voxcpm2",
            preview_only=payload.preview_only,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if job.status == "queued":
        try:
            PipelineLauncher(settings).launch_dubbing_job(job.id)
        except Exception as exc:
            raise HTTPException(
                502, f"YouDub dispatch failed; job is retained as queued: {type(exc).__name__}"
            ) from exc
    return RemoteJobResponse(job_id=job.id, status=job.status)


@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/retry",
             response_model=RemoteJobResponse)
def retry(
    task_id: uuid.UUID, job_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    ensure_ready(db)
    job = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.id == job_id,
    ).with_for_update().one_or_none()
    if job is None:
        raise HTTPException(404, "YouDub task not found")
    if (job.request_json or {}).get("model_path") != "youdub:voxcpm2":
        raise HTTPException(410, "Legacy dubbing has been retired; start a new YouDub job")
    task = db.get(Task, task_id)
    if task is None or task.status == TaskStatus.canceled:
        raise HTTPException(409, "source video is missing or canceled")
    if job.lease_owner and lease_is_active(job.lease_until):
        raise HTTPException(409, "worker lease is still active")
    if job.status not in ("failed", "paused", "queued", "preparing", "synthesizing",
                          "aligning", "mixing"):
        raise HTTPException(409, "this job cannot be resumed")
    job.lease_owner = job.lease_until = None
    job.status = "queued"
    job.error_message = None
    db.commit()
    try:
        PipelineLauncher(settings).launch_dubbing_job(
            job.id, dispatch_key=f"{job.id}:native-resume:{uuid.uuid4().hex}")
    except Exception as exc:
        raise HTTPException(502, f"native resume dispatch failed: {type(exc).__name__}") from exc
    return RemoteJobResponse(job_id=job.id, status="queued")


@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/control")
def control(
    task_id: uuid.UUID, job_id: uuid.UUID,
    action: str = Query(pattern="^(pause|cancel)$"),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    existing = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.id == job_id,
    ).one_or_none()
    if existing is None:
        raise HTTPException(404, "dubbing job not found")
    if (existing.request_json or {}).get("model_path") != "youdub:voxcpm2":
        raise HTTPException(410, "legacy dubbing records are read-only")
    try:
        job = request_control(db, task_id, job_id, action=action)
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": str(job.id), "status": job.status}


@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/rerun")
@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/confirm")
def retired_legacy_workflow(task_id: uuid.UUID, job_id: uuid.UUID) -> None:
    raise HTTPException(410, "Legacy previews and sentence rewriting have been removed. Create a new native YouDub job.")
