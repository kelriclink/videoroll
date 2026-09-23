from __future__ import annotations

import uuid

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.outbox.dispatcher import dispatch_outbox_events
from videoroll.apps.outbox.service import (
    claim_outbox_events,
    create_outbox_event,
    mark_outbox_dispatch_failed,
    redeliver_dispatched_event,
)
from videoroll.db.models import OutboxEvent


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    OutboxEvent.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        OutboxEvent.__table__.drop(engine)


def _pending_event(db: Session, *, operation_key: str = "task:batch:bilibili") -> OutboxEvent:
    return create_outbox_event(
        db,
        event_type="publish.bilibili",
        aggregate_type="publish_job",
        aggregate_id="job-1",
        task_name="bilibili_publisher.process_job",
        args={"args": ["job-1"], "queue": "publish"},
        operation_key=operation_key,
    )


def test_domain_commit_and_outbox_event_are_atomic(db: Session) -> None:
    event = _pending_event(db)

    assert db.query(OutboxEvent).filter_by(operation_key="task:batch:bilibili").one() is event


def test_duplicate_operation_key_reuses_pending_outbox_event(db: Session) -> None:
    first = _pending_event(db)
    second = _pending_event(db)

    assert second is first
    assert db.query(OutboxEvent).count() == 1


def test_expired_dispatch_lease_can_be_reclaimed(db: Session) -> None:
    event = _pending_event(db)
    now = event.available_at
    event.status = "dispatching"
    event.lease_owner = "old"
    event.lease_until = now - timedelta(seconds=1)
    db.flush()

    claimed = claim_outbox_events(db, owner="new", limit=1, now=now)

    assert claimed == [event]
    assert event.lease_owner == "new"
    assert event.status == "dispatching"


def test_broker_failure_releases_event_with_exponential_retry(db: Session) -> None:
    event = _pending_event(db)
    now = event.available_at
    claim_outbox_events(db, owner="dispatcher", limit=1, now=now)

    mark_outbox_dispatch_failed(db, event.id, owner="dispatcher", error="broker down", now=now)

    assert event.status == "pending"
    assert event.lease_owner is None
    assert event.available_at == now + timedelta(seconds=2)
    assert event.last_error == "broker down"


def test_safe_worker_nonstart_can_redeliver_an_already_dispatched_event(db: Session) -> None:
    event = _pending_event(db)
    event.status = "dispatched"
    event.lease_owner = None
    event.lease_until = None
    now = event.available_at + timedelta(seconds=1)

    assert redeliver_dispatched_event(db, event.operation_key, now=now) is True
    assert event.status == "pending"
    assert event.available_at == now
    assert event.last_error == "publisher worker never started; redelivering durable intent"


def test_dispatcher_sends_worker_args_and_outbox_event_id(db: Session) -> None:
    event = _pending_event(db)
    db.commit()
    celery_app = MagicMock()
    celery_app.send_task.return_value.id = "broker-message-1"

    result = dispatch_outbox_events(db, celery_app, owner="dispatcher", limit=10)

    assert result.dispatched == 1
    celery_app.send_task.assert_called_once_with(
        "bilibili_publisher.process_job",
        args=["job-1", str(event.id)],
        kwargs={},
        queue="publish",
    )
    assert db.get(OutboxEvent, event.id).status == "dispatched"


def test_workflow_cancel_bridge_calls_hatchet_and_marks_pipeline_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.apps.subtitle_service import worker
    from videoroll.db.models import PipelineRun

    pipeline_run_id = uuid.uuid4()
    response = MagicMock()
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.post.return_value = response
    finished: list[tuple[str, dict[str, object]]] = []
    row = SimpleNamespace(state="cancel_requested", error_message="old", finished_at=None)
    db = MagicMock()
    db.get.return_value = row

    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(worker, "_db", lambda: db)
    monkeypatch.setattr(
        worker,
        "_claim_outbox_worker_operation",
        lambda *_args, **_kwargs: (f"workflow-cancel:{pipeline_run_id}", "cancel-owner"),
    )
    monkeypatch.setattr(
        worker,
        "_finish_outbox_worker_operation",
        lambda operation_key, result: finished.append((operation_key, dict(result))),
    )
    monkeypatch.setattr(
        worker,
        "_release_outbox_worker_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("success must not release operation")),
    )
    monkeypatch.setattr(
        worker,
        "get_orchestrator_settings",
        lambda: SimpleNamespace(workflow_service_url="http://workflow-api:8030"),
    )
    monkeypatch.setattr(worker, "service_token", lambda _settings: "internal-token")
    monkeypatch.setattr(worker.httpx, "Client", lambda *args, **kwargs: client)

    result = worker.cancel_workflow_run.run(
        str(pipeline_run_id),
        "hatchet-run-1",
        "outbox-cancel-1",
    )

    client.post.assert_called_once_with("http://workflow-api:8030/runs/hatchet-run-1/cancel")
    db.get.assert_called_once_with(PipelineRun, pipeline_run_id)
    assert row.state == "canceled"
    assert row.finished_at is not None
    assert result == {
        "status": "ok",
        "pipeline_run_id": str(pipeline_run_id),
        "external_run_id": "hatchet-run-1",
    }
    assert finished == [(f"workflow-cancel:{pipeline_run_id}", result)]


def test_render_workflow_event_bridge_finishes_inbox_only_after_http_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.apps.subtitle_service import worker

    response = MagicMock()
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.post.return_value = response
    finished: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(
        worker,
        "_claim_outbox_worker_operation",
        lambda *_args, **_kwargs: ("workflow-render-finished:render-1:succeeded", "bridge-owner"),
    )
    monkeypatch.setattr(
        worker,
        "_finish_outbox_worker_operation",
        lambda operation_key, result: finished.append((operation_key, dict(result))),
    )
    monkeypatch.setattr(
        worker,
        "_release_outbox_worker_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("success must not release operation")),
    )
    monkeypatch.setattr(
        worker,
        "get_orchestrator_settings",
        lambda: SimpleNamespace(workflow_service_url="http://workflow-api:8030"),
    )
    monkeypatch.setattr(worker, "service_token", lambda _settings: "internal-token")
    monkeypatch.setattr(worker.httpx, "Client", lambda *args, **kwargs: client)

    result = worker.push_render_workflow_event.run(
        "render-1",
        "succeeded",
        "exec-1",
        None,
        "outbox-1",
    )

    client.post.assert_called_once_with(
        "http://workflow-api:8030/events/render-finished",
        json={
            "render_job_id": "render-1",
            "status": "succeeded",
            "execution_id": "exec-1",
            "error": None,
        },
    )
    assert result["status"] == "ok"
    assert finished == [
        (
            "workflow-render-finished:render-1:succeeded",
            {
                "status": "ok",
                "render_status": "succeeded",
                "render_job_id": "render-1",
                "execution_id": "exec-1",
                "error": None,
            },
        )
    ]


def test_publish_workflow_event_bridge_finishes_inbox_only_after_http_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.apps.subtitle_service import worker

    response = MagicMock()
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.post.return_value = response
    finished: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(
        worker,
        "_claim_outbox_worker_operation",
        lambda *_args, **_kwargs: ("workflow-publish-finished:batch-1:succeeded", "bridge-owner"),
    )
    monkeypatch.setattr(
        worker,
        "_finish_outbox_worker_operation",
        lambda operation_key, result: finished.append((operation_key, dict(result))),
    )
    monkeypatch.setattr(
        worker,
        "_release_outbox_worker_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("success must not release operation")),
    )
    monkeypatch.setattr(
        worker,
        "get_orchestrator_settings",
        lambda: SimpleNamespace(workflow_service_url="http://workflow-api:8030"),
    )
    monkeypatch.setattr(worker, "service_token", lambda _settings: "internal-token")
    monkeypatch.setattr(worker.httpx, "Client", lambda *args, **kwargs: client)

    result = worker.push_publish_workflow_event.run(
        "batch-1",
        "succeeded",
        "task-1",
        "outbox-2",
    )

    client.post.assert_called_once_with(
        "http://workflow-api:8030/events/publish-finished",
        json={
            "publish_batch_id": "batch-1",
            "status": "succeeded",
            "task_id": "task-1",
        },
    )
    assert result == {
        "status": "ok",
        "publish_status": "succeeded",
        "publish_batch_id": "batch-1",
        "task_id": "task-1",
    }
    assert finished == [
        (
            "workflow-publish-finished:batch-1:succeeded",
            result,
        )
    ]
