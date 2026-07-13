from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.remote_api_settings_store import (
    remote_api_token_is_configured,
    verify_remote_api_token,
)
from videoroll.apps.orchestrator_api.schemas import AutoYouTubeResponse, RemoteAutoYouTubeRequest
from videoroll.db.models import RemoteAPIRequest


logger = logging.getLogger(__name__)

_IDEMPOTENCY_KEY_MAX_LENGTH = 255
_TOKEN_MAX_LENGTH = 512
_REQUEST_TTL = timedelta(hours=24)
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_PER_TOKEN = 60
_RATE_LIMIT_PER_IP = 120
_MAX_CONCURRENT_DISPATCHES_PER_TOKEN = 4
_REDIS_PREFIX = "videoroll:remote-api:"


@dataclass(frozen=True)
class RemotePrincipal:
    """Authenticated identity represented only by a non-reversible token digest."""

    token_hash: str


@dataclass(frozen=True)
class RemoteAPIResponse:
    response: AutoYouTubeResponse
    replayed: bool = False


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_bearer_authorization(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "")
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or not token
        or token != token.strip()
        or len(token) > _TOKEN_MAX_LENGTH
        or not hmac.compare_digest(scheme.lower(), "bearer")
    ):
        raise HTTPException(status_code=401, detail="remote api requires an Authorization: Bearer token")
    return token


def authenticate_remote_request(request: Request, db: Session) -> RemotePrincipal:
    """Accept only a configured Bearer token; query tokens are never considered."""
    if not remote_api_token_is_configured(db):
        raise HTTPException(status_code=403, detail="remote api token is not set")
    token = _parse_bearer_authorization(request)
    # ``verify_remote_api_token`` uses PBKDF2 and hmac.compare_digest.  Do not
    # replace it with a direct string comparison or persist this plain token.
    if not verify_remote_api_token(db, token):
        raise HTTPException(status_code=401, detail="invalid remote api token")
    return RemotePrincipal(token_hash=_sha256_text(token))


def _normalized_idempotency_key(value: str | None) -> str:
    key = str(value or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is too long")
    return key


def _request_hash(payload: RemoteAutoYouTubeRequest) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(canonical)


def _record_expired(record: RemoteAPIRequest, now: datetime) -> bool:
    return record.lease_until is not None and _as_utc(record.lease_until) <= now


def _stored_response(record: RemoteAPIRequest) -> AutoYouTubeResponse:
    response_json = record.response_json if isinstance(record.response_json, dict) else None
    if record.status != "completed" or not response_json:
        raise HTTPException(status_code=409, detail="remote request is already being processed")
    try:
        return AutoYouTubeResponse.model_validate(response_json)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="remote request result is invalid") from exc


def _replay_or_conflict(record: RemoteAPIRequest, request_hash: str) -> RemoteAPIResponse:
    if not hmac.compare_digest(record.request_hash, request_hash):
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used with a different payload")
    if record.status == "failed":
        raise HTTPException(status_code=409, detail="remote request previously failed; use a new Idempotency-Key to retry")
    return RemoteAPIResponse(response=_stored_response(record), replayed=True)


def _find_record(db: Session, *, token_hash: str, idempotency_key: str) -> RemoteAPIRequest | None:
    return (
        db.query(RemoteAPIRequest)
        .filter(
            RemoteAPIRequest.token_hash == token_hash,
            RemoteAPIRequest.idempotency_key == idempotency_key,
        )
        .one_or_none()
    )


def _new_record(*, principal: RemotePrincipal, idempotency_key: str, request_hash: str, expires_at: datetime) -> RemoteAPIRequest:
    return RemoteAPIRequest(
        token_hash=principal.token_hash,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        # Payloads may contain private source/proof URLs.  The request hash is
        # sufficient for idempotency, so never persist the submitted JSON.
        request_json={},
        status="pending",
        lease_owner="remote-api",
        lease_until=expires_at,
    )


def _store_dispatch_failure(db: Session, record: RemoteAPIRequest) -> None:
    try:
        record.status = "failed"
        record.lease_owner = None
        record.response_json = {"detail": "pipeline dispatch failed"}
        record.completed_at = _now()
        db.add(record)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("failed to persist remote api dispatch failure")


def accept_remote_request(
    principal: RemotePrincipal,
    idempotency_key: str | None,
    payload: RemoteAutoYouTubeRequest,
    db: Session,
    *,
    dispatch: Callable[[], AutoYouTubeResponse] | None = None,
) -> RemoteAPIResponse:
    """Create a durable idempotency record, dispatch once, or replay its result.

    A committed database record is authoritative. Redis is intentionally not
    involved in replay/conflict decisions, so a Redis restart cannot produce a
    second pipeline dispatch.
    """
    key = _normalized_idempotency_key(idempotency_key)
    request_hash = _request_hash(payload)
    now = _now()
    existing = _find_record(db, token_hash=principal.token_hash, idempotency_key=key)
    if existing:
        if _record_expired(existing, now):
            db.delete(existing)
            db.commit()
        else:
            return _replay_or_conflict(existing, request_hash)

    record = _new_record(
        principal=principal,
        idempotency_key=key,
        request_hash=request_hash,
        expires_at=now + _REQUEST_TTL,
    )
    db.add(record)
    try:
        db.commit()
        db.refresh(record)
    except IntegrityError:
        db.rollback()
        existing = _find_record(db, token_hash=principal.token_hash, idempotency_key=key)
        if existing is None:
            raise HTTPException(status_code=503, detail="could not reserve remote request")
        return _replay_or_conflict(existing, request_hash)

    if dispatch is None:
        _store_dispatch_failure(db, record)
        raise HTTPException(status_code=500, detail="remote request dispatcher is not configured")

    try:
        response = dispatch()
    except Exception:
        _store_dispatch_failure(db, record)
        raise

    record.status = "completed"
    record.lease_owner = None
    record.response_json = response.model_dump(mode="json")
    record.completed_at = _now()
    db.add(record)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        # The external side effect already happened. Keep the durable pending
        # reservation rather than issuing a duplicate dispatch on a retry.
        raise HTTPException(status_code=503, detail="remote request result could not be persisted") from exc
    return RemoteAPIResponse(response=response)


def _redis_key(kind: str, digest: str) -> str:
    return f"{_REDIS_PREFIX}{kind}:{digest}"


def _request_ip_hash(request: Request) -> str:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "unknown") or "unknown")
    return _sha256_text(host)


def _increment_window(client: Redis, key: str) -> tuple[int, int]:
    count = int(client.incr(key))
    if count == 1:
        client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
    return count, max(1, int(client.ttl(key)))


def reserve_remote_dispatch_capacity(
    request: Request,
    principal: RemotePrincipal,
    *,
    redis_url: str,
) -> Callable[[], None]:
    """Use Redis only as a best-effort rate/concurrency guard around dispatch."""
    if not str(redis_url or "").strip():
        return lambda: None
    try:
        client = Redis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        token_count, token_ttl = _increment_window(client, _redis_key("rate-token", principal.token_hash))
        ip_count, ip_ttl = _increment_window(client, _redis_key("rate-ip", _request_ip_hash(request)))
        if token_count > _RATE_LIMIT_PER_TOKEN or ip_count > _RATE_LIMIT_PER_IP:
            raise HTTPException(
                status_code=429,
                detail="remote api rate limit exceeded",
                headers={"Retry-After": str(max(token_ttl, ip_ttl))},
            )
        active_key = _redis_key("active-dispatch", principal.token_hash)
        active_count = int(client.incr(active_key))
        if active_count == 1:
            client.expire(active_key, _RATE_LIMIT_WINDOW_SECONDS)
        if active_count > _MAX_CONCURRENT_DISPATCHES_PER_TOKEN:
            client.decr(active_key)
            raise HTTPException(status_code=429, detail="too many remote pipeline dispatches in progress")

        def release() -> None:
            try:
                client.decr(active_key)
            except (RedisError, OSError, ValueError, TypeError):
                logger.warning("remote api active-dispatch counter release failed")

        return release
    except HTTPException:
        raise
    except (RedisError, OSError, ValueError, TypeError):
        # This is an availability/throughput optimisation only. The database
        # record remains the source of truth for authentication/idempotency.
        logger.warning("remote api Redis quota guard unavailable; continuing with durable idempotency")
        return lambda: None
