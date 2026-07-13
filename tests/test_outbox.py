from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

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
