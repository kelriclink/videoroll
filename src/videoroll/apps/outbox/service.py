from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from videoroll.db.models import OutboxEvent


_MAX_LEASE_SECONDS = 300
_MAX_RETRY_SECONDS = 300


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_owner(owner: str) -> str:
    value = str(owner or "").strip()
    if not value:
        raise ValueError("outbox lease owner is required")
    return value[:128]


def _retry_delay(attempt_count: int) -> timedelta:
    # The first failed dispatch is retried after two seconds.  The cap keeps a
    # temporary broker outage recoverable without producing an unbounded date.
    seconds = min(_MAX_RETRY_SECONDS, 2 ** max(1, min(int(attempt_count or 0), 16)))
    return timedelta(seconds=seconds)


def create_outbox_event(
    db: Session,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | uuid.UUID,
    task_name: str,
    args: dict[str, Any],
    operation_key: str,
) -> OutboxEvent:
    """Add one durable event to the caller's current domain transaction.

    Reusing an operation key intentionally returns the existing event.  The
    inbox's database unique constraint remains the final duplicate-side-effect
    guard should two historical outbox rows ever exist during a rolling deploy.
    """
    key = str(operation_key or "").strip()
    if not key:
        raise ValueError("outbox operation_key is required")
    existing = (
        db.query(OutboxEvent)
        .filter(OutboxEvent.operation_key == key)
        .with_for_update()
        .order_by(OutboxEvent.created_at.desc())
        .first()
    )
    if existing is not None:
        return existing

    now = utcnow()
    event = OutboxEvent(
        event_type=str(event_type)[:128],
        aggregate_type=str(aggregate_type)[:128],
        aggregate_id=str(aggregate_id)[:128],
        task_name=str(task_name)[:255],
        args_json=dict(args or {}),
        operation_key=key[:255],
        status="pending",
        available_at=now,
        attempt_count=0,
    )
    db.add(event)
    # Allocate the UUID before the surrounding business transaction commits so
    # callers can include it in observability records without a separate save.
    db.flush()
    return event


def redeliver_dispatched_event(db: Session, operation_key: str, *, now: datetime | None = None) -> bool:
    """Make a broker-accepted-but-unstarted operation deliverable again.

    Callers must first prove that the domain operation did not reach its
    external side-effect boundary.  This intentionally does not disturb a
    live dispatcher lease; doing so would turn a broker optimisation into a
    correctness dependency.
    """
    key = str(operation_key or "").strip()
    if not key:
        return False
    event = (
        db.query(OutboxEvent)
        .filter(OutboxEvent.operation_key == key)
        .with_for_update()
        .order_by(OutboxEvent.created_at.desc())
        .first()
    )
    if event is None or event.status != "dispatched":
        return False
    now = now or utcnow()
    event.status = "pending"
    event.available_at = now
    event.lease_owner = None
    event.lease_until = None
    event.heartbeat_at = now
    event.last_error = "publisher worker never started; redelivering durable intent"
    db.add(event)
    db.flush()
    return True


def claim_outbox_events(
    db: Session,
    owner: str,
    limit: int,
    now: datetime | None = None,
) -> list[OutboxEvent]:
    """Claim due events with a bounded dispatcher lease.

    PostgreSQL honours ``SKIP LOCKED`` so multiple dispatcher processes make
    forward progress independently.  Other dialects retain the same state
    transitions for local tests and development.
    """
    owner = _bounded_owner(owner)
    now = now or utcnow()
    limit = max(1, min(int(limit or 1), 100))
    claimable = and_(
        OutboxEvent.available_at <= now,
        or_(
            OutboxEvent.status.in_(("pending", "failed")),
            and_(OutboxEvent.status == "dispatching", OutboxEvent.lease_until <= now),
        ),
    )
    events = (
        db.query(OutboxEvent)
        .filter(claimable)
        .order_by(OutboxEvent.available_at.asc(), OutboxEvent.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    lease_until = now + timedelta(seconds=_MAX_LEASE_SECONDS)
    for event in events:
        event.status = "dispatching"
        event.lease_owner = owner
        event.lease_until = lease_until
        event.heartbeat_at = now
        event.attempt_count = int(event.attempt_count or 0) + 1
        db.add(event)
    db.flush()
    return events


def mark_outbox_dispatched(db: Session, event_id: uuid.UUID | str, broker_id: str | None) -> None:
    """Record accepted broker delivery for an event currently being dispatched."""
    event = db.get(OutboxEvent, uuid.UUID(str(event_id)), with_for_update=True)
    if event is None:
        return
    event.status = "dispatched"
    event.lease_owner = None
    event.lease_until = None
    event.heartbeat_at = utcnow()
    event.delivered_at = event.heartbeat_at
    event.last_error = None
    # There is deliberately no broker-id column: broker result IDs are not a
    # correctness primitive.  Keeping delivery time and durable event ID is
    # sufficient for replay and tracing, while avoiding broker-specific state.
    del broker_id
    db.add(event)
    db.flush()


def mark_outbox_dispatch_failed(
    db: Session,
    event_id: uuid.UUID | str,
    *,
    owner: str,
    error: object,
    now: datetime | None = None,
) -> None:
    """Release a failed broker delivery for exponential-backoff retry."""
    owner = _bounded_owner(owner)
    now = now or utcnow()
    event = db.get(OutboxEvent, uuid.UUID(str(event_id)), with_for_update=True)
    if event is None or event.lease_owner != owner:
        return
    event.status = "pending"
    event.available_at = now + _retry_delay(int(event.attempt_count or 0))
    event.lease_owner = None
    event.lease_until = None
    event.heartbeat_at = now
    event.last_error = str(error or "broker delivery failed")[:1024]
    db.add(event)
    db.flush()
