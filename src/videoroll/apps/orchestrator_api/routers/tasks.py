from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_store, get_settings
from videoroll.apps.orchestrator_api.schemas import (
    AutoSubtitleHandoffResponse,
    ConvertedVideoItem,
    RecentFailedResumeResponse,
    RemoteJobResponse,
    SubtitleActionRequest,
    SubtitleJobSummary,
    TaskCreate,
    TaskBulkControlResponse,
    TaskRead,
)
from videoroll.apps.orchestrator_api.services import subtitle_service, task_service, youtube_service
from videoroll.config import OrchestratorSettings
from videoroll.db.models import SubtitleJob, Task, TaskStatus
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
