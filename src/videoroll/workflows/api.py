from __future__ import annotations

import hmac
import uuid

from hatchet_sdk import IdempotencyCollisionError
from functools import lru_cache
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from videoroll.workflows.auth import INTERNAL_TOKEN_HEADER, internal_service_token
from videoroll.workflows.client import create_hatchet_client
from videoroll.workflows.dubbing_job import DubbingJobWorkflowInput, register_dubbing_job_task
from videoroll.workflows.settings import WorkflowRuntimeSettings
from videoroll.workflows.video_pipeline import (
    AutoYouTubeWorkflowInput,
    PUBLISH_FINISHED_EVENT,
    RENDER_FINISHED_EVENT,
    DUBBING_FINISHED_EVENT,
    SubtitleJobWorkflowInput,
    register_subtitle_job_task,
    register_video_pipeline,
)


class AutoYouTubeLaunchRequest(BaseModel):
    task_id: str
    priority: int = 0
    dispatch_key: str | None = None


class SubtitleJobLaunchRequest(BaseModel):
    job_id: str
    priority: int = 0
    dispatch_key: str | None = None


class DubbingJobLaunchRequest(BaseModel):
    job_id: str
    priority: int = 0
    dispatch_key: str | None = None


class WorkflowLaunchResponse(BaseModel):
    workflow_run_id: str


class RenderFinishedEventRequest(BaseModel):
    render_job_id: str
    status: Literal["succeeded", "failed"]
    execution_id: str | None = None
    error: str | None = None


class DubbingFinishedEventRequest(BaseModel):
    dubbing_job_id: str
    status: Literal["succeeded", "failed"]
    error: str | None = None


class PublishFinishedEventRequest(BaseModel):
    publish_batch_id: str
    status: Literal["succeeded", "partial_failed", "failed"]
    task_id: str | None = None


@lru_cache(maxsize=1)
def _settings() -> WorkflowRuntimeSettings:
    return WorkflowRuntimeSettings.from_env()


@lru_cache(maxsize=1)
def _hatchet() -> Any:
    return create_hatchet_client()


@lru_cache(maxsize=1)
def _video_pipeline() -> Any:
    return register_video_pipeline(_hatchet(), settings=_settings())


@lru_cache(maxsize=1)
def _subtitle_job_task() -> Any:
    return register_subtitle_job_task(_hatchet())


@lru_cache(maxsize=1)
def _dubbing_job_task() -> Any:
    return register_dubbing_job_task(_hatchet())


def _require_internal(x_videoroll_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER)) -> None:
    settings = _settings()
    expected = internal_service_token(
        settings.internal_api_secret,
        development_mode=settings.development_mode,
    )
    presented = str(x_videoroll_internal_token or "").strip()
    if not presented:
        raise HTTPException(status_code=401, detail="internal service credentials required")
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="invalid internal service credentials")


def _hatchet_priority(value: int) -> Any:
    """Map VideoRoll's coarse priority buckets to Hatchet's native priority."""
    mapped = 3 if int(value) >= 50 else (1 if int(value) <= -50 else 2)
    try:
        from hatchet_sdk.types import Priority
    except ImportError:
        return mapped
    return {1: Priority.LOW, 2: Priority.MEDIUM, 3: Priority.HIGH}[mapped]


def create_app() -> FastAPI:
    app = FastAPI(title="videoroll-workflow", version="1")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs/auto-youtube", response_model=WorkflowLaunchResponse, dependencies=[Depends(_require_internal)])
    async def launch_auto_youtube(payload: AutoYouTubeLaunchRequest) -> WorkflowLaunchResponse:
        dispatch_key = payload.dispatch_key or f"{payload.task_id}:{uuid.uuid4().hex}"
        try:
            ref = await _video_pipeline().aio_run(
                AutoYouTubeWorkflowInput(task_id=payload.task_id, dispatch_key=dispatch_key),
                wait_for_result=False,
                additional_metadata={"videoroll_task_id": payload.task_id},
                priority=_hatchet_priority(payload.priority),
            )
            workflow_run_id = str(getattr(ref, "workflow_run_id", "") or "").strip()
        except IdempotencyCollisionError as exc:
            workflow_run_id = str(exc.existing_run_external_id or "").strip()
        if not workflow_run_id:
            raise HTTPException(status_code=502, detail="Hatchet did not return a workflow run id")
        return WorkflowLaunchResponse(workflow_run_id=workflow_run_id)

    @app.post("/runs/subtitle-job", response_model=WorkflowLaunchResponse, dependencies=[Depends(_require_internal)])
    async def launch_subtitle_job(payload: SubtitleJobLaunchRequest) -> WorkflowLaunchResponse:
        # Initial submissions get a unique key. Recovery retries reuse their
        # durable key so Hatchet does not start two runs after a lost response.
        dispatch_key = payload.dispatch_key or f"{payload.job_id}:{uuid.uuid4().hex}"
        try:
            ref = await _subtitle_job_task().aio_run(
                SubtitleJobWorkflowInput(job_id=payload.job_id, dispatch_key=dispatch_key),
                wait_for_result=False,
                additional_metadata={"videoroll_subtitle_job_id": payload.job_id},
                priority=_hatchet_priority(payload.priority),
            )
            workflow_run_id = str(getattr(ref, "workflow_run_id", "") or "").strip()
        except IdempotencyCollisionError as exc:
            workflow_run_id = str(exc.existing_run_external_id or "").strip()
        if not workflow_run_id:
            raise HTTPException(status_code=502, detail="Hatchet did not return a workflow run id")
        return WorkflowLaunchResponse(workflow_run_id=workflow_run_id)

    @app.post("/runs/dubbing-job", response_model=WorkflowLaunchResponse, dependencies=[Depends(_require_internal)])
    async def launch_dubbing_job(payload: DubbingJobLaunchRequest) -> WorkflowLaunchResponse:
        dispatch_key = payload.dispatch_key or f"{payload.job_id}:{uuid.uuid4().hex}"
        try:
            ref = await _dubbing_job_task().aio_run(
                DubbingJobWorkflowInput(job_id=payload.job_id, dispatch_key=dispatch_key),
                wait_for_result=False,
                additional_metadata={"videoroll_dubbing_job_id": payload.job_id},
                priority=_hatchet_priority(payload.priority),
            )
            workflow_run_id = str(getattr(ref, "workflow_run_id", "") or "").strip()
        except IdempotencyCollisionError as exc:
            workflow_run_id = str(exc.existing_run_external_id or "").strip()
        if not workflow_run_id:
            raise HTTPException(status_code=502, detail="Hatchet did not return a dubbing workflow run id")
        return WorkflowLaunchResponse(workflow_run_id=workflow_run_id)

    @app.post("/runs/{run_id}/cancel", dependencies=[Depends(_require_internal)])
    async def cancel_run(run_id: str) -> dict[str, str]:
        await _hatchet().runs.aio_cancel(run_id)
        return {"status": "canceled", "workflow_run_id": run_id}

    @app.post("/events/render-finished", dependencies=[Depends(_require_internal)])
    async def push_render_finished(payload: RenderFinishedEventRequest) -> dict[str, str]:
        await _hatchet().event.aio_push(
            RENDER_FINISHED_EVENT,
            payload.model_dump(),
            scope=payload.render_job_id,
        )
        return {"status": "accepted"}

    @app.post("/events/dubbing-finished", dependencies=[Depends(_require_internal)])
    async def push_dubbing_finished(payload: DubbingFinishedEventRequest) -> dict[str, str]:
        await _hatchet().event.aio_push(
            DUBBING_FINISHED_EVENT,
            payload.model_dump(),
            scope=payload.dubbing_job_id,
        )
        return {"status": "accepted"}

    @app.post("/events/publish-finished", dependencies=[Depends(_require_internal)])
    async def push_publish_finished(payload: PublishFinishedEventRequest) -> dict[str, str]:
        await _hatchet().event.aio_push(
            PUBLISH_FINISHED_EVENT,
            payload.model_dump(),
            scope=payload.publish_batch_id,
        )
        return {"status": "accepted"}

    return app


app = create_app()
