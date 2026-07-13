from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.db.models import OperationInbox, OutboxEvent


_MAX_LEASE_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_seconds(value: int) -> int:
    return max(1, min(int(value or 1), _MAX_LEASE_SECONDS))


@dataclass(frozen=True)
class OperationClaim:
    operation: OperationInbox
    acquired: bool

    @property
    def result_json(self) -> dict[str, Any] | None:
        value = self.operation.result_json
        return dict(value) if isinstance(value, dict) else None

    @property
    def is_active(self) -> bool:
        return self.operation.status == "processing" and self.operation.lease_until is not None


def _operation_for_update(db: Session, operation_key: str) -> OperationInbox | None:
    return (
        db.query(OperationInbox)
        .filter(OperationInbox.operation_key == operation_key)
        .with_for_update(skip_locked=True)
        .one_or_none()
    )


def claim_operation(
    db: Session,
    operation_key: str,
    owner: str,
    lease_seconds: int,
    *,
    request_json: dict[str, Any] | None = None,
) -> OperationClaim:
    """Claim one worker side effect, returning stored output for duplicates."""
    key = str(operation_key or "").strip()
    owner = str(owner or "").strip()[:128]
    if not key or not owner:
        raise ValueError("operation key and owner are required")
    now = _now()
    lease_until = now + timedelta(seconds=_lease_seconds(lease_seconds))
    operation = _operation_for_update(db, key)
    if operation is None:
        operation = OperationInbox(
            operation_key=key[:255],
            status="processing",
            request_json=dict(request_json or {}),
            lease_owner=owner,
            lease_until=lease_until,
            heartbeat_at=now,
        )
        try:
            with db.begin_nested():
                db.add(operation)
                db.flush()
            return OperationClaim(operation=operation, acquired=True)
        except IntegrityError:
            # A concurrent delivery won the unique operation-key race.  Read
            # its row under lock and follow the normal duplicate path.
            operation = _operation_for_update(db, key)
            if operation is None:
                raise

    if operation.status == "completed":
        return OperationClaim(operation=operation, acquired=False)
    if operation.lease_until is not None and operation.lease_until > now:
        return OperationClaim(operation=operation, acquired=False)

    operation.status = "processing"
    operation.lease_owner = owner
    operation.lease_until = lease_until
    operation.heartbeat_at = now
    operation.last_error = None
    db.add(operation)
    db.flush()
    return OperationClaim(operation=operation, acquired=True)


def claim_outbox_operation(
    db: Session,
    event_id: uuid.UUID | str,
    owner: str,
    lease_seconds: int,
) -> OperationClaim | None:
    event = db.get(OutboxEvent, uuid.UUID(str(event_id)))
    if event is None:
        return None
    return claim_operation(
        db,
        event.operation_key,
        owner,
        lease_seconds,
        request_json={"outbox_event_id": str(event.id), "event_type": event.event_type},
    )


def heartbeat_operation(db: Session, operation_key: str, owner: str, lease_seconds: int) -> bool:
    operation = _operation_for_update(db, str(operation_key))
    if operation is None or operation.status != "processing" or operation.lease_owner != str(owner):
        return False
    now = _now()
    operation.heartbeat_at = now
    operation.lease_until = now + timedelta(seconds=_lease_seconds(lease_seconds))
    db.add(operation)
    db.flush()
    return True


def finish_operation(db: Session, operation_key: str, result_json: dict[str, Any]) -> None:
    operation = _operation_for_update(db, str(operation_key))
    if operation is None:
        raise ValueError("operation not found")
    now = _now()
    operation.status = "completed"
    operation.result_json = dict(result_json or {})
    operation.lease_owner = None
    operation.lease_until = None
    operation.heartbeat_at = now
    operation.completed_at = now
    operation.last_error = None
    db.add(operation)
    db.flush()


def release_operation(db: Session, operation_key: str, owner: str, error: object) -> None:
    """Release a retryable operation when Celery schedules another attempt."""
    operation = _operation_for_update(db, str(operation_key))
    if operation is None or operation.lease_owner != str(owner):
        return
    operation.status = "pending"
    operation.lease_owner = None
    operation.lease_until = None
    operation.heartbeat_at = _now()
    operation.last_error = str(error or "worker retry")[:1024]
    db.add(operation)
    db.flush()


class OperationHeartbeat:
    """Refresh a worker lease from a separate short-lived DB session."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        operation_key: str,
        owner: str,
        lease_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._operation_key = operation_key
        self._owner = owner
        self._lease_seconds = _lease_seconds(lease_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="outbox-operation-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while not self._stop.wait(interval):
            db = self._session_factory()
            try:
                if heartbeat_operation(db, self._operation_key, self._owner, self._lease_seconds):
                    db.commit()
                else:
                    db.rollback()
                    return
            except Exception:
                db.rollback()
            finally:
                db.close()
