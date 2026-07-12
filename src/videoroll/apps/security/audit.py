from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import SecurityAuditEvent


_SENSITIVE_KEYS = {
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "set-cookie",
    "token",
}
_MAX_PAYLOAD_FIELDS = 16
_MAX_PAYLOAD_VALUE_LENGTH = 256


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _safe_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, raw_value in list((payload or {}).items())[:_MAX_PAYLOAD_FIELDS]:
        key = str(raw_key)[:64]
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            continue
        if isinstance(raw_value, bool) or raw_value is None:
            safe[key] = raw_value
        elif isinstance(raw_value, (int, float)):
            safe[key] = raw_value
        else:
            safe[key] = str(raw_value)[:_MAX_PAYLOAD_VALUE_LENGTH]
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
        request_id=_bounded_text(request_id, 128),
        source_ip=_bounded_text(source_ip, 64),
        payload_json=_safe_payload(payload),
        error_code=_bounded_text(error_code, 64),
        error_message=_bounded_text(error_message, 512),
    )


def write_security_audit(db: Session, **event_fields: Any) -> SecurityAuditEvent:
    event = build_security_audit_event(**event_fields)
    db.add(event)
    db.commit()
    return event
