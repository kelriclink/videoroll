from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import SecurityAuditEvent


_ALLOWED_PAYLOAD_COUNTERS = {"attempts", "retry_after"}


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _safe_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, raw_value in (payload or {}).items():
        key = str(raw_key)
        if key not in _ALLOWED_PAYLOAD_COUNTERS:
            continue
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            safe[key] = raw_value
    return safe


def build_security_audit_event(
    *,
    event_type: str,
    outcome: str,
    actor_type: str = "admin",
    actor_id: str | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
    payload: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> SecurityAuditEvent:
    return SecurityAuditEvent(
        event_type=str(event_type)[:128],
        actor_type=str(actor_type)[:64],
        actor_id=_bounded_text(actor_id, 128),
        outcome=str(outcome)[:32],
        # Request IDs and exception text can originate in request headers or
        # upstream services. Security events retain only structured fields.
        request_id=None,
        source_ip=_bounded_text(source_ip, 64),
        payload_json=_safe_payload(payload),
        error_code=_bounded_text(error_code, 64),
        error_message=None,
    )


def write_security_audit(db: Session, **event_fields: Any) -> SecurityAuditEvent:
    event = build_security_audit_event(**event_fields)
    db.add(event)
    db.commit()
    return event
