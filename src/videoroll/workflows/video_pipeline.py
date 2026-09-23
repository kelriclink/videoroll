from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from videoroll.workflows.auth import INTERNAL_TOKEN_HEADER, internal_service_token
from videoroll.workflows.settings import WorkflowRuntimeSettings


VIDEO_PIPELINE_NAME = "VideoPipelineV1"
SUBTITLE_JOB_TASK_NAME = "SubtitleJobV1"
RENDER_FINISHED_EVENT = "videoroll.render.finished"
PUBLISH_FINISHED_EVENT = "videoroll.publish.finished"


class AutoYouTubeWorkflowInput(BaseModel):
    task_id: str


class SubtitleJobWorkflowInput(BaseModel):
    job_id: str


class WorkflowStageOutput(BaseModel):
    task_id: str
    status: str
    detail: str = ""
    job_id: str | None = None
    job_kind: str | None = None
    render_job_id: str | None = None
    publish_batch_id: str | None = None


class RenderFinishedEvent(BaseModel):
    render_job_id: str
    status: str
    execution_id: str | None = None
    error: str | None = None


class PublishFinishedEvent(BaseModel):
    publish_batch_id: str
    status: str
    task_id: str | None = None


def _headers(settings: WorkflowRuntimeSettings) -> dict[str, str]:
    token = internal_service_token(
        settings.internal_api_secret,
        development_mode=settings.development_mode,
    )
    return {INTERNAL_TOKEN_HEADER: token} if token else {}


def _post_orchestrator(
    settings: WorkflowRuntimeSettings,
    path: str,
    *,
    timeout_seconds: float,
    retryable_statuses: set[int] | None = None,
) -> dict[str, Any]:
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers=_headers(settings),
        ) as client:
            response = client.post(f"{settings.orchestrator_url}{path}")
            response.raise_for_status()
            payload = response.json() if response.content else {}
            return payload if isinstance(payload, dict) else {"result": payload}
    except httpx.HTTPStatusError as exc:
        status = int(exc.response.status_code)
        detail = (exc.response.text or "").strip()
        retryable = retryable_statuses or {409, 429, 500, 502, 503, 504}
        if status not in retryable:
            try:
                from hatchet_sdk import NonRetryableException
            except ImportError:  # Keeps orchestrator/unit-test imports independent of Hatchet SDK.
                NonRetryableException = RuntimeError  # type: ignore[misc,assignment]
            raise NonRetryableException(f"orchestrator returned {status}: {detail[:1000]}") from exc
        raise RuntimeError(f"orchestrator returned retryable {status}: {detail[:1000]}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"orchestrator request failed: {type(exc).__name__}: {exc}") from exc


def _desired_pool(pool: str) -> list[Any]:
    try:
        from hatchet_sdk import DesiredWorkerLabel
    except ImportError:
        return []
    return [DesiredWorkerLabel(key="pool", value=pool, required=True)]


def _run_subtitle_job(job_id: str, *, retry_attempt: int) -> dict[str, str]:
    from videoroll.apps.subtitle_service.translation_stage import TranslationRetryRequired
    from videoroll.apps.subtitle_service.worker import SubtitleJobExecutionFailed, run_subtitle_job

    try:
        return run_subtitle_job(
            job_id,
            retry_attempt=retry_attempt,
            raise_on_error=True,
        )
    except TranslationRetryRequired:
        raise
    except SubtitleJobExecutionFailed as exc:
        try:
            from hatchet_sdk import NonRetryableException
        except ImportError:
            raise
        raise NonRetryableException(str(exc)) from exc


def register_subtitle_job_task(hatchet: Any) -> Any:
    @hatchet.task(
        name=SUBTITLE_JOB_TASK_NAME,
        input_validator=SubtitleJobWorkflowInput,
        retries=5,
        backoff_factor=2.0,
        backoff_max_seconds=60,
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(hours=12),
        desired_worker_labels=_desired_pool("subtitle"),
    )
    def subtitle_job(input: SubtitleJobWorkflowInput, ctx: Any) -> dict[str, str]:
        return _run_subtitle_job(input.job_id, retry_attempt=int(getattr(ctx, "retry_count", 0) or 0))

    return subtitle_job


def register_video_pipeline(hatchet: Any, *, settings: WorkflowRuntimeSettings | None = None) -> Any:
    runtime = settings or WorkflowRuntimeSettings.from_env()
    workflow = hatchet.workflow(
        name=VIDEO_PIPELINE_NAME,
        input_validator=AutoYouTubeWorkflowInput,
    )

    @workflow.task(
        name="youtube-download",
        retries=2,
        backoff_factor=2.0,
        backoff_max_seconds=30,
        schedule_timeout=timedelta(minutes=10),
        execution_timeout=timedelta(minutes=40),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    def youtube_download(input: AutoYouTubeWorkflowInput, _ctx: Any) -> WorkflowStageOutput:
        _post_orchestrator(
            runtime,
            f"/tasks/{input.task_id}/actions/youtube_download",
            timeout_seconds=35 * 60,
        )
        return WorkflowStageOutput(task_id=input.task_id, status="downloaded")

    @workflow.task(
        name="subtitle-handoff",
        parents=[youtube_download],
        retries=3,
        backoff_factor=2.0,
        backoff_max_seconds=20,
        schedule_timeout=timedelta(minutes=10),
        execution_timeout=timedelta(minutes=5),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    def subtitle_handoff(input: AutoYouTubeWorkflowInput, _ctx: Any) -> WorkflowStageOutput:
        payload = _post_orchestrator(
            runtime,
            f"/tasks/{input.task_id}/actions/auto_subtitle_handoff",
            timeout_seconds=240,
        )
        return WorkflowStageOutput(
            task_id=input.task_id,
            status=str(payload.get("status") or "queued"),
            detail=str(payload.get("detail") or ""),
            job_id=str(payload.get("job_id") or "").strip() or None,
            job_kind=str(payload.get("job_kind") or "").strip() or None,
        )

    @workflow.task(
        name="subtitle-process",
        parents=[subtitle_handoff],
        retries=5,
        backoff_factor=2.0,
        backoff_max_seconds=60,
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(hours=12),
        desired_worker_labels=_desired_pool("subtitle"),
    )
    def subtitle_process(input: AutoYouTubeWorkflowInput, ctx: Any) -> WorkflowStageOutput:
        handoff = ctx.task_output(subtitle_handoff)
        job_kind = getattr(handoff, "job_kind", None)
        job_id = getattr(handoff, "job_id", None)
        if isinstance(handoff, dict):
            job_kind = handoff.get("job_kind")
            job_id = handoff.get("job_id")
        if str(job_kind or "") == "render" and str(job_id or "").strip():
            return WorkflowStageOutput(
                task_id=input.task_id,
                status="skipped",
                detail="subtitle execution already completed; reusing render job",
                job_id=str(job_id).strip(),
                job_kind="render",
                render_job_id=str(job_id).strip(),
            )
        if str(job_kind or "") != "subtitle" or not str(job_id or "").strip():
            return WorkflowStageOutput(
                task_id=input.task_id,
                status="skipped",
                detail=f"subtitle execution not required (job_kind={job_kind or 'none'})",
                job_id=str(job_id or "").strip() or None,
                job_kind=str(job_kind or "").strip() or None,
            )
        result = _run_subtitle_job(
            str(job_id),
            retry_attempt=int(getattr(ctx, "retry_count", 0) or 0),
        )
        return WorkflowStageOutput(
            task_id=input.task_id,
            status=str(result.get("status") or "ok"),
            detail=str(result.get("detail") or ""),
            job_id=str(job_id),
            job_kind="subtitle",
            render_job_id=str(result.get("render_job_id") or "").strip() or None,
        )

    @workflow.durable_task(
        name="render-wait",
        parents=[subtitle_process],
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(hours=48),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    async def render_wait(input: AutoYouTubeWorkflowInput, ctx: Any) -> WorkflowStageOutput:
        subtitle_result = ctx.task_output(subtitle_process)
        render_job_id = getattr(subtitle_result, "render_job_id", None)
        if isinstance(subtitle_result, dict):
            render_job_id = subtitle_result.get("render_job_id")
        render_job_id = str(render_job_id or "").strip()
        if not render_job_id:
            return WorkflowStageOutput(
                task_id=input.task_id,
                status="skipped",
                detail="render not required",
            )

        event = await ctx.aio_wait_for_event(
            RENDER_FINISHED_EVENT,
            f"input.render_job_id == '{render_job_id}'",
            payload_validator=RenderFinishedEvent,
            scope=render_job_id,
            lookback_window=timedelta(hours=24),
            label=f"render:{render_job_id}",
        )
        if event.status != "succeeded":
            detail = event.error or f"render job {render_job_id} failed"
            try:
                from hatchet_sdk import NonRetryableException
            except ImportError:
                raise RuntimeError(detail)
            raise NonRetryableException(detail)
        return WorkflowStageOutput(
            task_id=input.task_id,
            status="rendered",
            detail="render completed",
            render_job_id=render_job_id,
        )

    @workflow.task(
        name="publish",
        parents=[render_wait],
        retries=2,
        backoff_factor=2.0,
        backoff_max_seconds=60,
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(hours=2),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    def publish(input: AutoYouTubeWorkflowInput, _ctx: Any) -> WorkflowStageOutput:
        result = _post_orchestrator(
            runtime,
            f"/tasks/{input.task_id}/actions/auto_publish",
            timeout_seconds=90 * 60,
            retryable_statuses={429, 500, 502, 503, 504},
        )
        status = str(result.get("status") or "ok")
        detail = str(result.get("detail") or "")
        if status == "error":
            raise RuntimeError(detail or "automatic publish failed")
        return WorkflowStageOutput(
            task_id=input.task_id,
            status=status,
            detail=detail,
            publish_batch_id=str(result.get("publish_batch_id") or "").strip() or None,
        )

    @workflow.durable_task(
        name="publish-wait",
        parents=[publish],
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(hours=48),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    async def publish_wait(input: AutoYouTubeWorkflowInput, ctx: Any) -> WorkflowStageOutput:
        publish_result = ctx.task_output(publish)
        publish_batch_id = getattr(publish_result, "publish_batch_id", None)
        if isinstance(publish_result, dict):
            publish_batch_id = publish_result.get("publish_batch_id")
        publish_batch_id = str(publish_batch_id or "").strip()
        if not publish_batch_id:
            return WorkflowStageOutput(
                task_id=input.task_id,
                status="skipped",
                detail="automatic publishing not required",
            )

        event = await ctx.aio_wait_for_event(
            PUBLISH_FINISHED_EVENT,
            f"input.publish_batch_id == '{publish_batch_id}'",
            payload_validator=PublishFinishedEvent,
            scope=publish_batch_id,
            lookback_window=timedelta(hours=24),
            label=f"publish:{publish_batch_id}",
        )
        if event.status != "succeeded":
            detail = f"publish batch {publish_batch_id} ended as {event.status}"
            try:
                from hatchet_sdk import NonRetryableException
            except ImportError:
                raise RuntimeError(detail)
            raise NonRetryableException(detail)
        return WorkflowStageOutput(
            task_id=input.task_id,
            status="published",
            detail="publish batch completed",
            publish_batch_id=publish_batch_id,
        )

    return workflow
