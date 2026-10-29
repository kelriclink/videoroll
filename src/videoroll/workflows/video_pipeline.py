from __future__ import annotations

import logging

from datetime import timedelta

from hatchet_sdk import TTLBasedIdempotencyConfig
from typing import Any

import httpx
from pydantic import BaseModel

from videoroll.workflows.auth import INTERNAL_TOKEN_HEADER, internal_service_token
from videoroll.workflows.settings import WorkflowRuntimeSettings


logger = logging.getLogger(__name__)

VIDEO_PIPELINE_NAME = "VideoPipelineV1"
SUBTITLE_JOB_TASK_NAME = "SubtitleJobV1"
RENDER_FINISHED_EVENT = "videoroll.render.finished"
DUBBING_FINISHED_EVENT = "videoroll.dubbing.finished"
PUBLISH_FINISHED_EVENT = "videoroll.publish.finished"
YOUTUBE_DOWNLOAD_REQUEST_TIMEOUT_SECONDS = 15
YOUTUBE_DOWNLOAD_POLL_INTERVAL_SECONDS = 60
YOUTUBE_DOWNLOAD_IDLE_GRACE_POLLS = 6
YOUTUBE_DOWNLOAD_MAX_POLLS = 48 * 60 * 60 // YOUTUBE_DOWNLOAD_POLL_INTERVAL_SECONDS


class AutoYouTubeWorkflowInput(BaseModel):
    task_id: str
    dispatch_key: str = ""


class SubtitleJobWorkflowInput(BaseModel):
    job_id: str
    dispatch_key: str = ""


class WorkflowStageOutput(BaseModel):
    task_id: str
    status: str
    detail: str = ""
    job_id: str | None = None
    job_kind: str | None = None
    render_job_id: str | None = None
    dubbing_job_id: str | None = None
    publish_batch_id: str | None = None


class RenderFinishedEvent(BaseModel):
    render_job_id: str
    status: str
    execution_id: str | None = None
    error: str | None = None


class DubbingFinishedEvent(BaseModel):
    dubbing_job_id: str
    status: str
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


def _non_retryable(message: str, *, cause: Exception | None = None) -> None:
    try:
        from hatchet_sdk import NonRetryableException
    except ImportError:
        NonRetryableException = RuntimeError  # type: ignore[misc,assignment]
    if cause is None:
        raise NonRetryableException(message)
    raise NonRetryableException(message) from cause


def _orchestrator_error(response: httpx.Response) -> tuple[str | None, str]:
    """Read machine-readable service errors without coupling control flow to prose."""
    message = (response.text or "").strip()
    code: str | None = None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            code = str(detail.get("code") or "").strip() or None
            message = str(detail.get("message") or message).strip()
        elif isinstance(detail, str):
            message = detail.strip()
    return code, message


async def _get_youtube_download_progress(
    settings: WorkflowRuntimeSettings,
    task_id: str,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers=_headers(settings),
        ) as client:
            response = await client.get(
                f"{settings.orchestrator_url}/tasks/{task_id}/youtube_download_progress"
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
            return payload if isinstance(payload, dict) else {}
    except httpx.HTTPStatusError as exc:
        status = int(exc.response.status_code)
        detail = (exc.response.text or "").strip()
        if status in {400, 401, 403, 404}:
            _non_retryable(
                f"youtube download progress returned {status}: {detail[:1000]}",
                cause=exc,
            )
        raise RuntimeError(
            f"youtube download progress returned retryable {status}: {detail[:1000]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"youtube download progress request failed: {type(exc).__name__}: {exc}"
        ) from exc


async def _wait_for_youtube_download(
    settings: WorkflowRuntimeSettings,
    task_id: str,
    ctx: Any,
    *,
    uncertain_start_error: Exception | None = None,
) -> dict[str, Any]:
    idle_polls = 0
    transient_progress_errors = 0
    for _ in range(YOUTUBE_DOWNLOAD_MAX_POLLS):
        try:
            progress = await _get_youtube_download_progress(settings, task_id)
            transient_progress_errors = 0
        except RuntimeError:
            transient_progress_errors += 1
            if transient_progress_errors >= YOUTUBE_DOWNLOAD_IDLE_GRACE_POLLS:
                raise
            await ctx.aio_sleep_for(timedelta(seconds=YOUTUBE_DOWNLOAD_POLL_INTERVAL_SECONDS))
            continue

        status = str(progress.get("status") or "").strip().lower()
        active = bool(progress.get("active"))
        if status == "completed":
            return progress
        if status == "failed":
            detail = str(progress.get("error") or "").strip()
            raise RuntimeError(detail or f"youtube download failed for task {task_id}")

        if status in {"preparing", "downloading", "processing", "storing", "uploading"} or active:
            idle_polls = 0
        elif status in {"", "idle"}:
            idle_polls += 1
            if uncertain_start_error is not None and idle_polls >= YOUTUBE_DOWNLOAD_IDLE_GRACE_POLLS:
                raise RuntimeError(
                    f"youtube download did not start after orchestrator request failed: "
                    f"{type(uncertain_start_error).__name__}: {uncertain_start_error}"
                ) from uncertain_start_error

        await ctx.aio_sleep_for(timedelta(seconds=YOUTUBE_DOWNLOAD_POLL_INTERVAL_SECONDS))

    raise RuntimeError(f"youtube download did not finish within 48 hours for task {task_id}")


async def _ensure_youtube_download(
    settings: WorkflowRuntimeSettings,
    task_id: str,
    ctx: Any,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(YOUTUBE_DOWNLOAD_REQUEST_TIMEOUT_SECONDS, connect=10.0),
            headers=_headers(settings),
        ) as client:
            response = await client.post(
                f"{settings.orchestrator_url}/tasks/{task_id}/actions/youtube_download_enqueue"
            )
    except httpx.HTTPError as exc:
        # A disconnected/expired HTTP request does not mean the synchronous
        # orchestrator download stopped. Durable-poll the business progress so
        # Hatchet does not start a duplicate download and burn retries on 409.
        return await _wait_for_youtube_download(
            settings,
            task_id,
            ctx,
            uncertain_start_error=exc,
        )

    if response.is_success:
        # An acknowledged durable enqueue is not a completed video download.
        return await _wait_for_youtube_download(settings, task_id, ctx)

    code, detail = _orchestrator_error(response)
    status = int(response.status_code)
    if status == 409 and code in {
        "YOUTUBE_DOWNLOAD_IN_PROGRESS",
        "YOUTUBE_DOWNLOAD_RESULT_RECOVERING",
        "YOUTUBE_DOWNLOAD_RECOVERY_IN_PROGRESS",
    }:
        return await _wait_for_youtube_download(settings, task_id, ctx)
    if status not in {429, 500, 502, 503, 504}:
        suffix = f" [{code}]" if code else ""
        _non_retryable(f"orchestrator returned {status}{suffix}: {detail[:1000]}")
    raise RuntimeError(f"orchestrator returned retryable {status}: {detail[:1000]}")


def _desired_pool(pool: str) -> list[Any]:
    try:
        from hatchet_sdk import DesiredWorkerLabel
    except ImportError:
        return []
    return [DesiredWorkerLabel(key="pool", value=pool, required=True)]


def _run_subtitle_job(job_id: str, *, retry_attempt: int) -> dict[str, str]:
    from videoroll.apps.subtitle_service.translation_stage import TranslationRetryRequired
    from videoroll.apps.subtitle_service.worker import (
        SubtitleJobExecutionFailed,
        SubtitleJobTransientFailure,
        run_subtitle_job,
    )

    try:
        return run_subtitle_job(
            job_id,
            retry_attempt=retry_attempt,
            raise_on_error=True,
        )
    except (TranslationRetryRequired, SubtitleJobTransientFailure):
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
        idempotency=TTLBasedIdempotencyConfig(
            key_expression="input.dispatch_key", ttl=timedelta(days=7),
        ),
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
        idempotency=TTLBasedIdempotencyConfig(
            key_expression="input.dispatch_key", ttl=timedelta(days=7),
        ),
    )

    @workflow.durable_task(
        name="youtube-download",
        retries=2,
        backoff_factor=2.0,
        backoff_max_seconds=30,
        schedule_timeout=timedelta(minutes=10),
        execution_timeout=timedelta(hours=48),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    async def youtube_download(input: AutoYouTubeWorkflowInput, ctx: Any) -> WorkflowStageOutput:
        await _ensure_youtube_download(runtime, input.task_id, ctx)
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
        # Scheduling wait and execution lifetime are independent.
        schedule_timeout=timedelta(hours=12),
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
            subtitle_status = getattr(subtitle_result, "status", None)
            if isinstance(subtitle_result, dict):
                subtitle_status = subtitle_result.get("status")
            if subtitle_status == "no_speech":
                return WorkflowStageOutput(
                    task_id=input.task_id, status="no_speech",
                    detail="No credible speech; do not render, dub or publish",
                )
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
        name="dubbing-start",
        parents=[render_wait],
        retries=3,
        backoff_factor=2.0,
        backoff_max_seconds=45,
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(minutes=5),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    def dubbing_start(input: AutoYouTubeWorkflowInput, _ctx: Any) -> WorkflowStageOutput:
        rendered = _ctx.task_output(render_wait)
        if getattr(rendered, "status", None) == "no_speech" or (
            isinstance(rendered, dict) and rendered.get("status") == "no_speech"
        ):
            return WorkflowStageOutput(task_id=input.task_id, status="no_speech",
                                       detail="No credible speech; dubbing skipped")
        result = _post_orchestrator(
            runtime,
            f"/tasks/{input.task_id}/actions/auto_dubbing",
            timeout_seconds=120,
        )
        return WorkflowStageOutput(
            task_id=input.task_id,
            status=str(result.get("status") or "queued"),
            detail=str(result.get("detail") or ""),
            dubbing_job_id=str(result.get("dubbing_job_id") or "").strip() or None,
        )

    @workflow.durable_task(
        name="dubbing-wait",
        parents=[dubbing_start],
        schedule_timeout=timedelta(days=7),
        execution_timeout=timedelta(days=7),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    async def dubbing_wait(input: AutoYouTubeWorkflowInput, ctx: Any) -> WorkflowStageOutput:
        started = ctx.task_output(dubbing_start)
        job_id = getattr(started, "dubbing_job_id", None)
        stage_status = getattr(started, "status", None)
        if isinstance(started, dict):
            job_id = started.get("dubbing_job_id")
            stage_status = started.get("status")
        job_id = str(job_id or "").strip()
        if not job_id:
            if str(stage_status or "") == "no_speech":
                return WorkflowStageOutput(task_id=input.task_id, status="no_speech",
                                           detail="No credible speech; publishing must be skipped")
            return WorkflowStageOutput(task_id=input.task_id, status="skipped",
                                       detail="automatic dubbing disabled")
        if str(stage_status or "") == "succeeded":
            return WorkflowStageOutput(task_id=input.task_id, status="dubbed", dubbing_job_id=job_id)
        event = await ctx.aio_wait_for_event(
            DUBBING_FINISHED_EVENT,
            f"input.dubbing_job_id == '{job_id}'",
            payload_validator=DubbingFinishedEvent,
            scope=job_id,
            lookback_window=timedelta(days=7),
            label=f"dubbing:{job_id}",
        )
        if event.status != "succeeded":
            # A resumed job can have a durable 'failed: paused' event left by
            # the earlier canceled attempt. Ignore that event ONLY while the
            # current persisted job is actively resuming; never mask a real
            # failure whose job status is already terminal.
            reason = str(event.error or "").lower()
            if event.status == "failed" and "paused" in reason:
                from uuid import UUID
                from videoroll.config import get_orchestrator_settings
                from videoroll.db.models import DubbingJob
                from videoroll.db.session import get_sessionmaker
                with get_sessionmaker(get_orchestrator_settings().database_url)() as lookup:
                    current = lookup.get(DubbingJob, UUID(job_id))
                    resumed = bool(current is not None and current.status in (
                        "queued", "preparing", "synthesizing", "aligning", "mixing", "rendering",
                    ))
                if resumed:
                    event = await ctx.aio_wait_for_event(
                        DUBBING_FINISHED_EVENT,
                        f"input.dubbing_job_id == '{job_id}' && input.status == 'succeeded'",
                        payload_validator=DubbingFinishedEvent,
                        scope=f"{job_id}:resumed",
                        lookback_window=timedelta(days=7),
                        label=f"dubbing-resumed:{job_id}",
                    )
            if event.status != "succeeded":
                _non_retryable(event.error or f"YouDub task {job_id} ended as {event.status}")
        return WorkflowStageOutput(task_id=input.task_id, status="dubbed",
                                   detail="YouDub finished; P1/P2 ready",
                                   dubbing_job_id=job_id)

    @workflow.task(
        name="publish",
        parents=[dubbing_wait],
        retries=2,
        backoff_factor=2.0,
        backoff_max_seconds=60,
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(hours=2),
        desired_worker_labels=_desired_pool("workflow-core"),
    )
    def publish(input: AutoYouTubeWorkflowInput, ctx: Any) -> WorkflowStageOutput:
        dubbed = ctx.task_output(dubbing_wait)
        status = getattr(dubbed, "status", None)
        if isinstance(dubbed, dict):
            status = dubbed.get("status")
        if status == "no_speech":
            return WorkflowStageOutput(task_id=input.task_id, status="no_speech",
                                       detail="Automatic publishing suppressed: no credible speech")
        job_id = getattr(dubbed, "dubbing_job_id", None)
        if isinstance(dubbed, dict):
            job_id = dubbed.get("dubbing_job_id")
        route = f"/tasks/{input.task_id}/actions/auto_publish"
        if job_id:
            route += f"?dubbing_job_id={job_id}"
        result = _post_orchestrator(
            runtime,
            route,
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

    @workflow.on_success_task(name="pipeline-audit-success")
    def pipeline_audit_success(input: AutoYouTubeWorkflowInput, _ctx: Any) -> dict[str, str]:
        from videoroll.workflows.pipeline_status_sync import finish_auto_pipeline_audit
        try:
            finish_auto_pipeline_audit(input.task_id, input.dispatch_key, succeeded=True)
        except Exception:
            logger.exception("VideoPipeline success audit callback failed; reconciler will recover")
            return {"status": "audit_deferred"}
        return {"status": "succeeded"}

    @workflow.on_failure_task(name="pipeline-audit-failure")
    def pipeline_audit_failure(input: AutoYouTubeWorkflowInput, _ctx: Any) -> dict[str, str]:
        from videoroll.workflows.pipeline_status_sync import finish_auto_pipeline_audit
        try:
            finish_auto_pipeline_audit(input.task_id, input.dispatch_key, succeeded=False)
        except Exception:
            logger.exception("VideoPipeline failure audit callback failed; reconciler will recover")
            return {"status": "audit_deferred"}
        return {"status": "failed"}

    return workflow
