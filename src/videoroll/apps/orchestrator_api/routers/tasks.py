from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_store, get_settings
from videoroll.apps.orchestrator_api.schemas import (
    AutoSubtitleHandoffResponse,
    ConvertedVideoItem,
    DubbingActionRequest,
    RecentFailedResumeResponse,
    RemoteJobResponse,
    SubtitleActionRequest,
    SubtitleJobSummary,
    SubtitleRetranslateRequest,
    TaskCreate,
    TaskBulkControlResponse,
    TaskRead,
)
from videoroll.apps.orchestrator_api.services import subtitle_service, task_service, youtube_service
from videoroll.config import OrchestratorSettings
from videoroll.db.models import DubbingJob, SubtitleJob, Task, TaskStatus
from videoroll.realtime import publish_queue_changed
from videoroll.storage.filesystem import FileStore


router = APIRouter()


@router.post("/tasks", response_model=TaskRead)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)) -> Task:
    return task_service.create_task(payload, db=db)


@router.get("/videos/converted", response_model=list[ConvertedVideoItem])
def list_converted_videos(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return task_service.list_converted_videos(limit=limit, db=db)


@router.get("/tasks", response_model=list[TaskRead])
def list_tasks(
    status: TaskStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> list[dict[str, Any]]:
    return task_service.list_tasks(status=status, limit=limit, db=db, store=store)


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> dict[str, Any]:
    return task_service.get_task(task_id, db=db, store=store)


def _restart_resumed_auto_youtube_task(
    task: Task,
    *,
    settings: OrchestratorSettings,
    db: Session,
) -> None:
    should_restart_auto_youtube, auto_publish = task_service.auto_youtube_restart_options(task, db=db)
    if should_restart_auto_youtube:
        youtube_service.enqueue_auto_youtube_pipeline(
            task.id,
            auto_publish=auto_publish,
            settings=settings,
        )


@router.post("/tasks/{task_id}/actions/stop", response_model=TaskRead)
def stop_task(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Task:
    task = task_service.stop_task(task_id, db=db)
    publish_queue_changed(settings.redis_url, task_id=task.id)
    return task


@router.post("/tasks/{task_id}/actions/resume", response_model=TaskRead)
def resume_stopped_task(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Task:
    task = task_service.resume_stopped_task(task_id, db=db)
    _restart_resumed_auto_youtube_task(task, settings=settings, db=db)
    subtitle_service.relaunch_queued_subtitle_job(task.id, settings=settings, db=db)
    return task


@router.post("/tasks/actions/stop_all", response_model=TaskBulkControlResponse)
def stop_all_tasks(
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TaskBulkControlResponse:
    matched_count, changed_count = task_service.stop_all_tasks(db=db)
    publish_queue_changed(settings.redis_url)
    return TaskBulkControlResponse(matched_count=matched_count, changed_count=changed_count)


@router.post("/tasks/actions/resume_stopped", response_model=TaskBulkControlResponse)
def resume_all_stopped_tasks(
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TaskBulkControlResponse:
    matched_count, changed_count, tasks = task_service.resume_all_stopped_tasks(db=db)
    for task in tasks:
        _restart_resumed_auto_youtube_task(task, settings=settings, db=db)
        subtitle_service.relaunch_queued_subtitle_job(task.id, settings=settings, db=db)
    return TaskBulkControlResponse(matched_count=matched_count, changed_count=changed_count)


@router.get("/tasks/{task_id}/subtitle_jobs", response_model=list[SubtitleJobSummary])
def list_task_subtitle_jobs(
    task_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[SubtitleJob]:
    return subtitle_service.list_task_subtitle_jobs(task_id, limit=limit, db=db)


@router.post("/tasks/{task_id}/actions/auto_subtitle_handoff", response_model=AutoSubtitleHandoffResponse)
def auto_subtitle_handoff(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AutoSubtitleHandoffResponse:
    return subtitle_service.enqueue_auto_subtitle_handoff(
        task_id,
        settings=settings,
        db=db,
    )


@router.post("/tasks/{task_id}/actions/subtitle", response_model=RemoteJobResponse)
def enqueue_subtitle_job(
    task_id: uuid.UUID,
    payload: SubtitleActionRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> RemoteJobResponse:
    return subtitle_service.enqueue_subtitle_job(task_id, payload, settings=settings, db=db, store=store)


@router.get("/tasks/{task_id}/dubbing_jobs")
def list_task_dubbing_jobs(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    jobs = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id,
    ).order_by(DubbingJob.created_at.desc()).limit(50).all()
    return [
        {"id": str(j.id), "task_id": str(j.task_id),
         "subtitle_job_id": str(j.subtitle_job_id) if j.subtitle_job_id else None,
         "status": j.status, "progress": j.progress,
         "operation_key": j.operation_key, "request": j.request_json,
         "error_message": j.error_message, "output": j.output_json,
         "created_at": j.created_at.isoformat() if j.created_at else None}
        for j in jobs
    ]


@router.post("/tasks/{task_id}/actions/dubbing", response_model=RemoteJobResponse)
def enqueue_dubbing_job(
    task_id: uuid.UUID,
    payload: DubbingActionRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    from videoroll.apps.dubbing.job import create_dubbing_job
    from videoroll.apps.dubbing.model_management import installed, COSY_MODELS
    from videoroll.apps.dubbing.worker_status import worker_status
    from videoroll.workflows.launcher import PipelineLauncher
    if not worker_status()["available"]:
        raise HTTPException(503, "dubbing worker is not running; enable it before submitting")
    if not installed(payload.model_path):
        raise HTTPException(409, "Selected TTS model is not installed")
    if payload.model_path in COSY_MODELS:
        from videoroll.apps.dubbing.cosyvoice3_adapter import runtime_ready
        if not runtime_ready():
            raise HTTPException(503, "CosyVoice3 inference runtime is not installed")
    if payload.mix_mode == "background_only":
        from videoroll.apps.dubbing.separation import available
        if not available():
            raise HTTPException(503, "Demucs separation is not installed; use replace or original_low")

    try:
        job = create_dubbing_job(
            db, task_id=task_id, subtitle_job_id=payload.subtitle_job_id,
            subtitle_video_key=payload.subtitle_video_key,
            operation_key=payload.operation_key, voice=payload.voice,
            model_path=payload.model_path,
            retain_original_audio=payload.retain_original_audio,
            preview_only=payload.preview_only,
            instruct=payload.instruct,
            temperature=payload.temperature,
            top_p=payload.top_p,
            batch_size=payload.batch_size,
            mix_mode=payload.mix_mode,
            reference_profile_id=payload.reference_profile_id,
            cosy_mode=payload.cosy_mode,
            cosy_speed=payload.cosy_speed,
            debug_timing=payload.debug_timing,
            max_audio_speed=payload.max_audio_speed,
            auto_group_speech=payload.auto_group_speech,
            auto_rewrite_speech=payload.auto_rewrite_speech,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job.status in {"queued", "failed"}:
        try:
            PipelineLauncher(settings).launch_dubbing_job(job.id)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"dubbing dispatch failed, persisted job={job.id}: {type(exc).__name__}",
            ) from exc
    return RemoteJobResponse(job_id=job.id, status=job.status)


@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/retry", response_model=RemoteJobResponse)
def retry_dubbing_job(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    """Explicit operator recovery for failed or expired-lease dubbing runs."""
    from videoroll.workflows.launcher import PipelineLauncher
    from videoroll.apps.dubbing.job import lease_is_active
    from videoroll.apps.dubbing.worker_status import worker_status

    job = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.id == job_id,
    ).with_for_update().one_or_none()
    if job is None:
        raise HTTPException(404, "dubbing job not found")
    task = db.get(Task, task_id)
    if task is None or task.status == TaskStatus.canceled:
        raise HTTPException(409, "task is stopped or missing")
    if job.status in {"succeeded", "canceled"}:
        raise HTTPException(409, "completed or canceled dubbing job cannot be retried")
    if job.lease_owner and lease_is_active(job.lease_until):
        raise HTTPException(409, "dubbing worker is still within its active lease")
    if job.status not in {"queued", "failed", "paused", "needs_review", "preparing", "synthesizing", "aligning", "mixing"}:
        raise HTTPException(409, f"dubbing job cannot be retried from {job.status}")
    if not worker_status()["available"]:
        raise HTTPException(503, "dubbing worker is not running")
    job.lease_owner = None
    job.lease_until = None
    job.status = "queued"
    job.error_message = None
    db.commit()
    try:
        PipelineLauncher(settings).launch_dubbing_job(
            job.id, dispatch_key=f"{job.id}:manual-retry:{uuid.uuid4().hex}",
        )
    except Exception as exc:
        raise HTTPException(
            502, f"dubbing recovery dispatch failed; job still queued: {type(exc).__name__}",
        ) from exc
    return RemoteJobResponse(job_id=job.id, status="queued")



@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/rerun", response_model=RemoteJobResponse)
def rerun_dubbing_with_parameters(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    payload: DubbingActionRequest,
    regenerate: bool = Query(False),
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    """Regenerate a debug preview or start the full run without a new job ID."""
    from videoroll.apps.dubbing.reconfigure import reconfigure_dubbing
    from videoroll.apps.dubbing.worker_status import worker_status
    from videoroll.workflows.launcher import PipelineLauncher
    if not worker_status()['available']:
        raise HTTPException(503,'dubbing worker or model service unavailable')
    try:
        job=reconfigure_dubbing(db,task_id=task_id,job_id=job_id,
            values=payload.model_dump(),regenerate=regenerate)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    try:
        PipelineLauncher(settings).launch_dubbing_job(
            job.id,dispatch_key=f'{job.id}:debug-rerun:{uuid.uuid4().hex}')
    except Exception as exc:
        raise HTTPException(502,f'dubbing was queued but dispatch failed: {type(exc).__name__}') from exc
    return RemoteJobResponse(job_id=job.id,status='queued')


@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/confirm", response_model=RemoteJobResponse)
def confirm_dubbing_preview(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    from videoroll.workflows.launcher import PipelineLauncher
    from videoroll.apps.dubbing.worker_status import worker_status
    if not worker_status()["available"]:
        raise HTTPException(503, "dubbing worker is not running")
    job = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.id == job_id,
    ).with_for_update().one_or_none()
    if job is None:
        raise HTTPException(404, "dubbing job not found")
    if job.status != "preview_ready":
        raise HTTPException(409, "only a completed preview can be confirmed")
    # Historical previews may carry a timing_issue written by the retired
    # strict per-segment alignment engine. YouDub handles that same WAV by
    # global/local stretch and timeline continuation, without script editing.
    output = dict(job.output_json or {})
    output.pop("timing_issue", None)
    job.output_json = output
    request = dict(job.request_json or {})
    request["preview_only"] = False
    job.request_json = request
    job.status = "queued"
    job.lease_owner = None
    job.lease_until = None
    db.commit()
    PipelineLauncher(settings).launch_dubbing_job(
        job.id, dispatch_key=f"{job.id}:preview-confirm:{uuid.uuid4().hex}",
    )
    return RemoteJobResponse(job_id=job.id, status="queued")


@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/control")
def control_dubbing_job(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    action: str = Query(pattern="^(pause|cancel)$"),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    from videoroll.apps.dubbing.control import request_control
    try:
        job = request_control(db, task_id, job_id, action=action)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": str(job.id), "status": job.status}




@router.post("/tasks/{task_id}/actions/subtitle_retranslate", response_model=RemoteJobResponse)
def retranslate_subtitle_indices(
    task_id: uuid.UUID,
    payload: SubtitleRetranslateRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    return subtitle_service.enqueue_selective_subtitle_retranslation(
        task_id,
        payload,
        settings=settings,
        db=db,
    )


@router.post("/tasks/{task_id}/actions/subtitle_resume", response_model=RemoteJobResponse)
def resume_subtitle_job(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    return subtitle_service.resume_subtitle_job(task_id, settings=settings, db=db)


@router.post("/tasks/actions/resume_failed_recent", response_model=RecentFailedResumeResponse)
def resume_recent_failed_tasks(
    window_hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=200, ge=1, le=500),
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> RecentFailedResumeResponse:
    return subtitle_service.resume_recent_failed_tasks(
        window_hours=window_hours,
        limit=limit,
        settings=settings,
        db=db,
        store=store,
    )
