from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.outbox.service import (
    claim_outbox_events,
    mark_outbox_dispatch_failed,
    mark_outbox_dispatched,
)
from videoroll.db.models import OutboxEvent


@dataclass(frozen=True)
class DispatchResult:
    claimed: int = 0
    dispatched: int = 0
    failed: int = 0


def _message_parts(event: OutboxEvent) -> tuple[list[Any], dict[str, Any], str | None]:
    payload = dict(event.args_json or {})
    raw_args = payload.get("args", [])
    if not isinstance(raw_args, list):
        raise ValueError("outbox args_json.args must be a list")
    raw_kwargs = payload.get("kwargs", {})
    if not isinstance(raw_kwargs, dict):
        raise ValueError("outbox args_json.kwargs must be an object")
    queue = payload.get("queue")
    if queue is not None and not isinstance(queue, str):
        raise ValueError("outbox args_json.queue must be a string")
    return list(raw_args), dict(raw_kwargs), queue


def dispatch_outbox_events(
    db: Session,
    celery_app: Any,
    *,
    owner: str,
    limit: int = 25,
) -> DispatchResult:
    """Deliver claimed events and persist broker failures for later retry."""
    events = claim_outbox_events(db, owner=owner, limit=limit)
    if not events:
        return DispatchResult()

    # The lease must survive a dispatcher crash before broker I/O begins.
    db.commit()
    dispatched = 0
    failed = 0
    for event in events:
        try:
            args, kwargs, queue = _message_parts(event)
            args.append(str(event.id))
            result = celery_app.send_task(event.task_name, args=args, kwargs=kwargs, queue=queue)
            mark_outbox_dispatched(db, event.id, str(getattr(result, "id", "") or ""))
            db.commit()
            dispatched += 1
        except Exception as exc:
            db.rollback()
            mark_outbox_dispatch_failed(db, event.id, owner=owner, error=exc)
            db.commit()
            failed += 1
    return DispatchResult(claimed=len(events), dispatched=dispatched, failed=failed)
