from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from videoroll.workflows import api as workflow_api
from videoroll.workflows.auth import INTERNAL_TOKEN_HEADER, internal_service_token


class _FakePipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def aio_run(self, payload: Any, **kwargs: Any) -> Any:
        self.calls.append((payload, dict(kwargs)))
        return SimpleNamespace(workflow_run_id="hatchet-run-1")


class _FakeEvents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def aio_push(self, key: str, payload: dict[str, Any], *, scope: str | None = None, **_kwargs: Any) -> Any:
        self.calls.append((key, dict(payload), scope))
        return object()


class _FakeRuns:
    def __init__(self) -> None:
        self.canceled: list[str] = []

    async def aio_cancel(self, run_id: str) -> None:
        self.canceled.append(run_id)


def _runtime() -> Any:
    return SimpleNamespace(
        internal_api_secret="workflow-test-secret",
        development_mode=False,
    )


def test_workflow_api_health_does_not_require_hatchet_credentials(monkeypatch) -> None:
    def fail_if_called() -> Any:
        raise AssertionError("Hatchet client must not be initialized by /health")

    monkeypatch.setattr(workflow_api, "_hatchet", fail_if_called)
    response = TestClient(workflow_api.create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_workflow_api_requires_internal_service_token(monkeypatch) -> None:
    runtime = _runtime()
    fake_pipeline = _FakePipeline()
    fake_subtitle_task = _FakePipeline()
    fake_events = _FakeEvents()
    fake_runs = _FakeRuns()
    monkeypatch.setattr(workflow_api, "_settings", lambda: runtime)
    monkeypatch.setattr(workflow_api, "_video_pipeline", lambda: fake_pipeline)
    monkeypatch.setattr(workflow_api, "_subtitle_job_task", lambda: fake_subtitle_task)
    monkeypatch.setattr(workflow_api, "_hatchet", lambda: SimpleNamespace(event=fake_events, runs=fake_runs))
    client = TestClient(workflow_api.create_app())

    unauthorized = client.post("/runs/auto-youtube", json={"task_id": "task-1"})
    assert unauthorized.status_code == 401

    token = internal_service_token(runtime.internal_api_secret, development_mode=False)
    response = client.post(
        "/runs/auto-youtube",
        json={"task_id": "task-1", "priority": 100},
        headers={INTERNAL_TOKEN_HEADER: token},
    )
    assert response.status_code == 200
    assert response.json() == {"workflow_run_id": "hatchet-run-1"}
    assert len(fake_pipeline.calls) == 1
    payload, options = fake_pipeline.calls[0]
    assert payload.task_id == "task-1"
    assert options["wait_for_result"] is False
    assert options["additional_metadata"] == {"videoroll_task_id": "task-1"}
    assert int(options["priority"]) == 3

    subtitle_response = client.post(
        "/runs/subtitle-job",
        json={"job_id": "job-1", "priority": -50},
        headers={INTERNAL_TOKEN_HEADER: token},
    )
    assert subtitle_response.status_code == 200
    assert subtitle_response.json() == {"workflow_run_id": "hatchet-run-1"}
    subtitle_payload, subtitle_options = fake_subtitle_task.calls[0]
    assert subtitle_payload.job_id == "job-1"
    assert subtitle_options["wait_for_result"] is False
    assert subtitle_options["additional_metadata"] == {"videoroll_subtitle_job_id": "job-1"}
    assert int(subtitle_options["priority"]) == 1

    cancel_response = client.post(
        "/runs/hatchet-run-1/cancel",
        headers={INTERNAL_TOKEN_HEADER: token},
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json() == {"status": "canceled", "workflow_run_id": "hatchet-run-1"}
    assert fake_runs.canceled == ["hatchet-run-1"]

    event_response = client.post(
        "/events/render-finished",
        json={
            "render_job_id": "render-1",
            "status": "succeeded",
            "execution_id": "exec-1",
            "error": None,
        },
        headers={INTERNAL_TOKEN_HEADER: token},
    )
    assert event_response.status_code == 200
    assert event_response.json() == {"status": "accepted"}
    assert fake_events.calls == [
        (
            workflow_api.RENDER_FINISHED_EVENT,
            {"render_job_id": "render-1", "status": "succeeded", "execution_id": "exec-1", "error": None},
            "render-1",
        )
    ]

    publish_event_response = client.post(
        "/events/publish-finished",
        json={
            "publish_batch_id": "batch-1",
            "status": "succeeded",
            "task_id": "task-1",
        },
        headers={INTERNAL_TOKEN_HEADER: token},
    )
    assert publish_event_response.status_code == 200
    assert publish_event_response.json() == {"status": "accepted"}
    assert fake_events.calls[-1] == (
        workflow_api.PUBLISH_FINISHED_EVENT,
        {"publish_batch_id": "batch-1", "status": "succeeded", "task_id": "task-1"},
        "batch-1",
    )
