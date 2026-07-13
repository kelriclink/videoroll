from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.db.models import DesktopAccessGrant


DesktopType = Literal["login", "publish"]

DEFAULT_DESKTOP_GRANT_TTL_SECONDS = 300
MAX_DESKTOP_GRANT_TTL_SECONDS = 900
DEFAULT_DESKTOP_GRANT_RECONNECT_LIMIT = 8
MAX_DESKTOP_GRANT_RECONNECT_LIMIT = 12


class DesktopGrantCreate(BaseModel):
    desktop_type: DesktopType
    resource_id: str = Field(min_length=1, max_length=128)


class DesktopGrantRead(BaseModel):
    token: str
    desktop_type: DesktopType
    resource_id: str
    expires_at: datetime
    reconnect_limit: int


def desktop_session_fingerprint(admin_session: str) -> str:
    """Return a stable, non-reversible identity for a trusted-device cookie."""
    session = str(admin_session or "").strip()
    if not session:
        raise ValueError("administrator session is required")
    return hashlib.sha256(f"videoroll-desktop-session:v1:{session}".encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _desktop_type(value: str) -> DesktopType:
    normalized = str(value or "").strip().lower()
    if normalized not in {"login", "publish"}:
        raise ValueError("unsupported desktop type")
    return normalized  # type: ignore[return-value]


def _resource_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("desktop resource_id must be a UUID") from exc


def _bounded_positive(value: int, *, default: int, maximum: int, name: str) -> int:
    numeric = int(value if value is not None else default)
    if numeric < 1 or numeric > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return numeric


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _read_scope(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def create_desktop_grant(
    db: Session,
    admin_session: str,
    desktop_type: DesktopType,
    resource_id: str,
    *,
    ttl_seconds: int = DEFAULT_DESKTOP_GRANT_TTL_SECONDS,
    reconnect_limit: int = DEFAULT_DESKTOP_GRANT_RECONNECT_LIMIT,
) -> DesktopGrantRead:
    """Create a short-lived desktop grant without persisting its raw token."""
    session_fingerprint = desktop_session_fingerprint(admin_session)
    normalized_type = _desktop_type(desktop_type)
    normalized_resource_id = _resource_id(resource_id)
    ttl = _bounded_positive(
        ttl_seconds,
        default=DEFAULT_DESKTOP_GRANT_TTL_SECONDS,
        maximum=MAX_DESKTOP_GRANT_TTL_SECONDS,
        name="ttl_seconds",
    )
    limit = _bounded_positive(
        reconnect_limit,
        default=DEFAULT_DESKTOP_GRANT_RECONNECT_LIMIT,
        maximum=MAX_DESKTOP_GRANT_RECONNECT_LIMIT,
        name="reconnect_limit",
    )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    # A collision is cryptographically negligible, but retrying makes the
    # database uniqueness constraint authoritative instead of an assumption.
    for _attempt in range(3):
        token = secrets.token_urlsafe(32)
        grant = DesktopAccessGrant(
            token_hash=_token_hash(token),
            subject=session_fingerprint,
            scope_json={
                "desktop_type": normalized_type,
                "resource_id": normalized_resource_id,
                "session_fingerprint": session_fingerprint,
                "reconnect_limit": limit,
                "reconnect_count": 0,
            },
            status="active",
            expires_at=expires_at,
        )
        db.add(grant)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        return DesktopGrantRead(
            token=token,
            desktop_type=normalized_type,
            resource_id=normalized_resource_id,
            expires_at=expires_at,
            reconnect_limit=limit,
        )
    raise RuntimeError("unable to create desktop grant")


def _desktop_scope_from_original_uri(request: Request) -> tuple[DesktopType, str, str, bool]:
    original_uri = str(request.headers.get("x-original-uri") or "").strip()
    parsed = urlsplit(original_uri)
    path = parsed.path.rstrip("/")
    if path.startswith("/social-login/"):
        desktop_type: DesktopType = "login"
    elif path.startswith("/social-publish/"):
        desktop_type = "publish"
    else:
        raise HTTPException(status_code=403, detail="desktop path is not authorized")

    query = parse_qs(parsed.query, keep_blank_values=False)
    token = str((query.get("grant") or [request.headers.get("x-desktop-grant") or ""])[0]).strip()
    resource_id = str((query.get("resource") or [request.headers.get("x-desktop-resource") or ""])[0]).strip()
    if not token or not resource_id:
        raise HTTPException(status_code=403, detail="desktop grant is required")
    try:
        # noVNC loads several HTTP assets before it opens one WebSocket.  Every
        # asset is authorized, while only the WebSocket spends a reconnect.
        return desktop_type, _resource_id(resource_id), token, path.endswith("/websockify")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="desktop resource is invalid") from exc


def _consume_desktop_grant(
    db: Session,
    *,
    admin_session: str,
    desktop_type: DesktopType,
    resource_id: str,
    token: str,
    consume_reconnect: bool,
) -> bool:
    fingerprint = desktop_session_fingerprint(admin_session)
    grant = (
        db.query(DesktopAccessGrant)
        .filter(DesktopAccessGrant.token_hash == _token_hash(token))
        .with_for_update()
        .one_or_none()
    )
    if grant is None or grant.status != "active" or grant.subject != fingerprint:
        return False

    now = datetime.now(timezone.utc)
    if _utc(grant.expires_at) <= now:
        grant.status = "expired"
        grant.last_error = "expired"
        db.add(grant)
        db.commit()
        return False

    scope = _read_scope(grant.scope_json)
    if (
        scope.get("desktop_type") != desktop_type
        or scope.get("resource_id") != resource_id
        or scope.get("session_fingerprint") != fingerprint
    ):
        return False
    if not consume_reconnect:
        return True
    try:
        reconnect_limit = _bounded_positive(
            int(scope.get("reconnect_limit") or 0),
            default=DEFAULT_DESKTOP_GRANT_RECONNECT_LIMIT,
            maximum=MAX_DESKTOP_GRANT_RECONNECT_LIMIT,
            name="reconnect_limit",
        )
        reconnect_count = int(scope.get("reconnect_count") or 0)
    except (TypeError, ValueError):
        grant.status = "revoked"
        grant.last_error = "invalid_scope"
        db.add(grant)
        db.commit()
        return False
    if reconnect_count < 0 or reconnect_count >= reconnect_limit:
        grant.status = "consumed"
        grant.last_error = "reconnect_limit_reached"
        db.add(grant)
        db.commit()
        return False

    scope["reconnect_count"] = reconnect_count + 1
    grant.scope_json = scope
    grant.consumed_at = now
    if reconnect_count + 1 >= reconnect_limit:
        grant.status = "consumed"
    db.add(grant)
    db.commit()
    return True


def authorize_desktop_request(request: Request) -> None:
    """Authorize the internal Nginx subrequest for one noVNC connection."""
    db = getattr(request.state, "desktop_grant_db", None)
    admin_session = str(getattr(request.state, "admin_session", "") or "").strip()
    if not isinstance(db, Session) or not admin_session:
        raise HTTPException(status_code=401, detail="administrator session is required")
    desktop_type, resource_id, token, consume_reconnect = _desktop_scope_from_original_uri(request)
    if not _consume_desktop_grant(
        db,
        admin_session=admin_session,
        desktop_type=desktop_type,
        resource_id=resource_id,
        token=token,
        consume_reconnect=consume_reconnect,
    ):
        raise HTTPException(status_code=403, detail="desktop grant is invalid or expired")
