from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
from typing import Any, Callable

from videoroll.workflows.settings import WorkflowRuntimeSettings
from videoroll.workflows.video_pipeline import (
    AutoYouTubeWorkflowInput,
    PUBLISH_FINISHED_EVENT,
    RENDER_FINISHED_EVENT,
    PublishFinishedEvent,
    RenderFinishedEvent,
    VIDEO_PIPELINE_NAME,
    register_video_pipeline,
)


class _FakeWorkflow:
    def __init__(self) -> None:
        self.tasks: dict[str, Callable[..., Any]] = {}
        self.options: dict[str, dict[str, Any]] = {}

    def task(self, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            name = str(kwargs.get("name") or fn.__name__)
            self.tasks[name] = fn
            self.options[name] = dict(kwargs)
            return fn

        return decorator

    def durable_task(self, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kwargs = {**kwargs, "durable": True}
        return self.task(**kwargs)


class _FakeHatchet:
    def __init__(self) -> None:
        self.workflow_name = ""
        self.input_validator: object | None = None
        self.workflow_obj = _FakeWorkflow()

    def workflow(self, *, name: str, input_validator: object, **_kwargs: Any) -> _FakeWorkflow:
        self.workflow_name = name
        self.input_validator = input_validator
        return self.workflow_obj


class _FakeContext:
    def __init__(
        self,
        parent_output: Any,
        *,
        retry_count: int = 0,
        render_event: RenderFinishedEvent | None = None,
        publish_event: PublishFinishedEvent | None = None,
    ) -> None:
        self.parent_output = parent_output
        self.retry_count = retry_count
        self.render_event = render_event
        self.publish_event = publish_event
        self.wait_calls: list[tuple[str, str | None, dict[str, Any]]] = []
        self.sleep_calls: list[timedelta] = []

    def task_output(self, _task: object) -> Any:
        return self.parent_output

    async def aio_wait_for_event(self, key: str, expression: str | None = None, **kwargs: Any) -> Any:
        self.wait_calls.append((key, expression, dict(kwargs)))
        if key == RENDER_FINISHED_EVENT:
            assert self.render_event is not None
            return self.render_event
        if key == PUBLISH_FINISHED_EVENT:
            assert self.publish_event is not None
            return self.publish_event
        raise AssertionError(f"unexpected event key: {key}")

    async def aio_sleep_for(self, duration: timedelta) -> None:
        self.sleep_calls.append(duration)


def test_video_pipeline_orders_download_handoff_and_subtitle_process(monkeypatch) -> None:
    from videoroll.workflows import video_pipeline as module

    calls: list[tuple[str, float]] = []

    def fake_post(
        _settings: WorkflowRuntimeSettings,
        path: str,
        *,
        timeout_seconds: float,
        retryable_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        del retryable_statuses
        calls.append((path, timeout_seconds))
        if path.endswith("/actions/auto_subtitle_handoff"):
            return {"status": "queued", "detail": "ok", "job_id": "job-1", "job_kind": "subtitle"}
        if path.endswith("/actions/auto_publish"):
            return {"status": "ok", "detail": "publish submitted", "publish_batch_id": "batch-1"}
        return {"status": "downloaded", "detail": "ok"}

    monkeypatch.setattr(module, "_post_orchestrator", fake_post)
    download_calls: list[str] = []

    async def fake_ensure_download(
        _settings: WorkflowRuntimeSettings,
        task_id: str,
        _ctx: Any,
    ) -> dict[str, Any]:
        download_calls.append(task_id)
        return {"status": "completed"}

    monkeypatch.setattr(module, "_ensure_youtube_download", fake_ensure_download)
    subtitle_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        module,
        "_run_subtitle_job",
        lambda job_id, retry_attempt: subtitle_calls.append((job_id, retry_attempt)) or {
            "status": "ok",
            "detail": "render queued",
            "render_job_id": "render-1",
        },
    )
    hatchet = _FakeHatchet()
    settings = WorkflowRuntimeSettings(
        orchestrator_url="http://orchestrator:8000",
        internal_api_secret="test-secret",
        development_mode=True,
    )

    workflow = register_video_pipeline(hatchet, settings=settings)

    assert workflow is hatchet.workflow_obj
    assert hatchet.workflow_name == VIDEO_PIPELINE_NAME
    assert hatchet.input_validator is AutoYouTubeWorkflowInput
    assert set(workflow.tasks) == {"youtube-download", "subtitle-handoff", "subtitle-process", "render-wait", "publish", "publish-wait"}
    assert workflow.options["youtube-download"]["retries"] == 2
    assert workflow.options["youtube-download"]["durable"] is True
    assert workflow.options["subtitle-handoff"]["retries"] == 3
    assert workflow.options["subtitle-handoff"]["parents"] == [workflow.tasks["youtube-download"]]
    assert workflow.options["subtitle-process"]["parents"] == [workflow.tasks["subtitle-handoff"]]
    assert workflow.options["subtitle-process"]["retries"] == 5
    assert workflow.options["render-wait"]["parents"] == [workflow.tasks["subtitle-process"]]
    assert workflow.options["render-wait"]["durable"] is True
    assert workflow.options["publish"]["parents"] == [workflow.tasks["render-wait"]]
    assert workflow.options["publish-wait"]["parents"] == [workflow.tasks["publish"]]
    assert workflow.options["publish-wait"]["durable"] is True

    payload = AutoYouTubeWorkflowInput(task_id="00000000-0000-0000-0000-000000000001")
    download = asyncio.run(workflow.tasks["youtube-download"](payload, _FakeContext(None)))
    handoff = workflow.tasks["subtitle-handoff"](payload, object())
    subtitle = workflow.tasks["subtitle-process"](payload, _FakeContext(handoff, retry_count=2))
    render_ctx = _FakeContext(
        subtitle,
        render_event=RenderFinishedEvent(
            render_job_id="render-1",
            status="succeeded",
            execution_id="exec-1",
        ),
    )
    rendered = asyncio.run(workflow.tasks["render-wait"](payload, render_ctx))
    published = workflow.tasks["publish"](payload, _FakeContext(rendered))
    publish_ctx = _FakeContext(
        published,
        publish_event=PublishFinishedEvent(
            publish_batch_id="batch-1",
            status="succeeded",
            task_id=payload.task_id,
        ),
    )
    finished = asyncio.run(workflow.tasks["publish-wait"](payload, publish_ctx))

    assert download.status == "downloaded"
    assert handoff.status == "queued"
    assert handoff.job_id == "job-1"
    assert subtitle.status == "ok"
    assert subtitle.job_id == "job-1"
    assert subtitle.render_job_id == "render-1"
    assert rendered.status == "rendered"
    assert rendered.render_job_id == "render-1"
    assert published.status == "ok"
    assert published.detail == "publish submitted"
    assert published.publish_batch_id == "batch-1"
    assert finished.status == "published"
    assert finished.publish_batch_id == "batch-1"
    assert render_ctx.wait_calls[0][0] == RENDER_FINISHED_EVENT
    assert "render-1" in str(render_ctx.wait_calls[0][1])
    assert render_ctx.wait_calls[0][2]["scope"] == "render-1"
    assert publish_ctx.wait_calls[0][0] == PUBLISH_FINISHED_EVENT
    assert "batch-1" in str(publish_ctx.wait_calls[0][1])
    assert publish_ctx.wait_calls[0][2]["scope"] == "batch-1"
    assert subtitle_calls == [("job-1", 2)]
    assert download_calls == [payload.task_id]
    assert calls == [
        ("/tasks/00000000-0000-0000-0000-000000000001/actions/auto_subtitle_handoff", 240),
        ("/tasks/00000000-0000-0000-0000-000000000001/actions/auto_publish", 90 * 60),
    ]


def test_youtube_download_409_waits_for_existing_operation(monkeypatch) -> None:
    from videoroll.workflows import video_pipeline as module

    request = httpx.Request("POST", "http://orchestrator:8000/tasks/task-1/actions/youtube_download")

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, _url: str) -> httpx.Response:
            return httpx.Response(
                409,
                request=request,
                json={"detail": "youtube download is already in progress"},
            )

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    waited: list[str] = []

    async def fake_wait(
        _settings: WorkflowRuntimeSettings,
        task_id: str,
        _ctx: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        waited.append(task_id)
        return {"status": "completed", "progress": 100}

    monkeypatch.setattr(module, "_wait_for_youtube_download", fake_wait)
    settings = WorkflowRuntimeSettings(orchestrator_url="http://orchestrator:8000", development_mode=True)
    result = asyncio.run(module._ensure_youtube_download(settings, "task-1", _FakeContext(None)))

    assert result["status"] == "completed"
    assert waited == ["task-1"]


def test_youtube_download_transport_timeout_checks_progress_before_retry(monkeypatch) -> None:
    from videoroll.workflows import video_pipeline as module

    request = httpx.Request("POST", "http://orchestrator:8000/tasks/task-2/actions/youtube_download")

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, _url: str) -> httpx.Response:
            raise httpx.ReadTimeout("download response timed out", request=request)

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    seen_error: list[type[Exception]] = []

    async def fake_wait(
        _settings: WorkflowRuntimeSettings,
        _task_id: str,
        _ctx: Any,
        *,
        uncertain_start_error: Exception | None = None,
    ) -> dict[str, Any]:
        assert uncertain_start_error is not None
        seen_error.append(type(uncertain_start_error))
        return {"status": "completed", "progress": 100}

    monkeypatch.setattr(module, "_wait_for_youtube_download", fake_wait)
    settings = WorkflowRuntimeSettings(orchestrator_url="http://orchestrator:8000", development_mode=True)
    result = asyncio.run(module._ensure_youtube_download(settings, "task-2", _FakeContext(None)))

    assert result["status"] == "completed"
    assert seen_error == [httpx.ReadTimeout]
