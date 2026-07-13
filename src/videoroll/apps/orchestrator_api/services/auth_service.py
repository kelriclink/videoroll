from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.admin_auth_store import (
    DEVICE_COOKIE_NAME,
    device_cookie_max_age_seconds,
    encode_password_hash,
    get_password_hash,
    mint_device_cookie_value,
    set_password_hash,
    validate_new_password,
    verify_device_cookie_value,
    verify_password_hash,
)
from videoroll.apps.orchestrator_api.schemas import (
    AdminAuthLoginRequest,
    AdminAuthSetupRequest,
    AdminAuthStatusRead,
)
from videoroll.apps.security.audit import write_security_audit
from videoroll.apps.security.rate_limits import (
    RateLimitDecision,
    RateLimitUnavailable,
    check_login_rate_limit,
    clear_login_rate_limit,
    record_login_failure,
)
from videoroll.apps.security.service_auth import (
    ADMIN_BOOTSTRAP_HEADER,
    consume_bootstrap_secret,
)
from videoroll.db.session import get_sessionmaker


def secure_cookie(request: Request) -> bool:
    proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    return proto == "https"


def set_device_cookie(response: Response, value: str, *, secure: bool) -> None:
    response.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=value,
        max_age=device_cookie_max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=bool(secure),
        path="/",
    )


def _source_ip(request: Request) -> str:
    peer_text = str(getattr(request.client, "host", "") or "").strip()
    try:
        peer = ipaddress.ip_address(peer_text)
    except ValueError:
        return "unknown"

    raw_cidrs = str(
        getattr(getattr(getattr(request, "app", None), "state", None), "trusted_proxy_cidrs", "")
        or ""
    )
    trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_cidr in raw_cidrs.split(",")[:32]:
        cidr = raw_cidr.strip()
        if not cidr:
            continue
        try:
            trusted_networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue

    def is_trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(address.version == network.version and address in network for network in trusted_networks)

    if not trusted_networks or not is_trusted(peer):
        return peer.compressed

    forwarded_values = [
        part.strip()
        for part in str(request.headers.get("x-forwarded-for") or "").split(",")
    ]
    if not forwarded_values or not all(forwarded_values):
        return peer.compressed
    try:
        forwarded = [ipaddress.ip_address(value) for value in forwarded_values]
    except ValueError:
        return peer.compressed

    candidate = peer
    for hop in reversed(forwarded):
        if not is_trusted(candidate):
            break
        candidate = hop
    return candidate.compressed


def _request_id(request: Request) -> str | None:
    value = str(request.headers.get("x-request-id") or "").strip()
    return value[:128] or None


def _rate_limit_key(request: Request, endpoint: str) -> str:
    return f"{endpoint}:{_source_ip(request)}"


def _redis_url(request: Request) -> str:
    return str(getattr(request.app.state, "redis_url", "") or "").strip()


def _audit(
    db: Session,
    request: Request,
    *,
    event_type: str,
    outcome: str,
    error_code: str | None = None,
    error_message: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    bounded_payload = dict(payload or {})
    bounded_payload["user_agent"] = str(request.headers.get("user-agent") or "")[:256]
    write_security_audit(
        db,
        event_type=event_type,
        outcome=outcome,
        request_id=_request_id(request),
        source_ip=_source_ip(request),
        error_code=error_code,
        error_message=error_message,
        payload=bounded_payload,
    )


def _check_rate_limit(request: Request, db: Session, endpoint: str) -> str:
    redis_url = _redis_url(request)
    if not redis_url:
        _audit(
            db,
            request,
            event_type=f"admin.{endpoint}.failure",
            outcome="failure",
            error_code="rate_limiter_unavailable",
        )
        raise HTTPException(status_code=503, detail="authentication rate limiter unavailable")
    key = _rate_limit_key(request, endpoint)
    try:
        decision = check_login_rate_limit(redis_url, key)
    except RateLimitUnavailable as exc:
        _audit(
            db,
            request,
            event_type=f"admin.{endpoint}.failure",
            outcome="failure",
            error_code="rate_limiter_unavailable",
            error_message=str(exc),
        )
        raise HTTPException(status_code=503, detail="authentication rate limiter unavailable") from exc
    if not decision.allowed:
        try:
            extended = record_login_failure(redis_url, key)
            if not extended.allowed:
                decision = extended
        except RateLimitUnavailable:
            pass
        _audit(
            db,
            request,
            event_type=f"admin.{endpoint}.throttle",
            outcome="throttled",
            error_code="rate_limited",
            payload={"attempts": decision.attempts, "retry_after": decision.retry_after},
        )
        raise HTTPException(
            status_code=429,
            detail="too many authentication attempts",
            headers={"Retry-After": str(decision.retry_after)},
        )
    return key


def _record_failure(request: Request, db: Session, endpoint: str, key: str, error_code: str) -> None:
    decision = RateLimitDecision(allowed=True)
    try:
        decision = record_login_failure(_redis_url(request), key)
    except RateLimitUnavailable as exc:
        _audit(
            db,
            request,
            event_type=f"admin.{endpoint}.failure",
            outcome="failure",
            error_code="rate_limiter_unavailable",
            error_message=str(exc),
        )
        raise HTTPException(status_code=503, detail="authentication rate limiter unavailable") from exc
    if not decision.allowed:
        _audit(
            db,
            request,
            event_type=f"admin.{endpoint}.throttle",
            outcome="throttled",
            error_code="rate_limited",
            payload={"attempts": decision.attempts, "retry_after": decision.retry_after},
        )
        raise HTTPException(
            status_code=429,
            detail="too many authentication attempts",
            headers={"Retry-After": str(decision.retry_after)},
        )
    _audit(
        db,
        request,
        event_type=f"admin.{endpoint}.failure",
        outcome="failure",
        error_code=error_code,
        payload={"attempts": decision.attempts},
    )


def _clear_rate_limit(request: Request, key: str) -> None:
    try:
        clear_login_rate_limit(_redis_url(request), key)
    except RateLimitUnavailable:
        pass


def require_bootstrap_secret(request: Request) -> None:
    presented = str(request.headers.get(ADMIN_BOOTSTRAP_HEADER) or "").strip()
    consume_bootstrap_secret(request, presented)


def get_admin_password_hash(request: Request, db: Session | None = None) -> str:
    cached = str(getattr(request.app.state, "admin_password_hash", "") or "").strip()
    if cached:
        return cached

    password_hash = ""
    if db is not None:
        try:
            password_hash = str(get_password_hash(db) or "").strip()
        except Exception:
            password_hash = ""
    else:
        database_url = str(getattr(request.app.state, "database_url", "") or "").strip()
        if database_url:
            session_local = get_sessionmaker(database_url)
            fallback_db = session_local()
            try:
                password_hash = str(get_password_hash(fallback_db) or "").strip()
            finally:
                fallback_db.close()

    if password_hash:
        request.app.state.admin_password_hash = password_hash
    return password_hash


def auth_status(request: Request, db: Session) -> AdminAuthStatusRead:
    password_hash = get_admin_password_hash(request, db)
    password_set = bool(password_hash)
    trusted = False
    if password_set:
        cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
        cookie_value = str(request.cookies.get(DEVICE_COOKIE_NAME) or "").strip()
        if cookie_secret and cookie_value:
            trusted = verify_device_cookie_value(
                cookie_value,
                internal_secret=cookie_secret,
                password_hash=password_hash,
            )
    return AdminAuthStatusRead(password_set=password_set, trusted=trusted)


def setup_auth(payload: AdminAuthSetupRequest, request: Request, db: Session) -> JSONResponse:
    rate_key = _check_rate_limit(request, db, "setup")
    if get_admin_password_hash(request, db):
        _record_failure(request, db, "setup", rate_key, "password_already_set")
        raise HTTPException(status_code=400, detail="admin password already set")

    try:
        password = validate_new_password(payload.password)
    except ValueError:
        _record_failure(request, db, "setup", rate_key, "invalid_password")
        raise

    request.state.bootstrap_db = db
    try:
        require_bootstrap_secret(request)
        encoded = encode_password_hash(password)
        set_password_hash(db, encoded)
    except HTTPException:
        db.rollback()
        _record_failure(request, db, "setup", rate_key, "invalid_bootstrap_secret")
        raise
    finally:
        request.state.bootstrap_db = None
    request.app.state.admin_password_hash = encoded
    _clear_rate_limit(request, rate_key)
    _audit(db, request, event_type="admin.setup.success", outcome="success")

    cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
    cookie_value = mint_device_cookie_value(internal_secret=cookie_secret, password_hash=encoded)
    body = AdminAuthStatusRead(password_set=True, trusted=True).model_dump(mode="json")
    response = JSONResponse(status_code=200, content=body)
    set_device_cookie(response, cookie_value, secure=secure_cookie(request))
    return response


def login(payload: AdminAuthLoginRequest, request: Request, db: Session) -> JSONResponse:
    rate_key = _check_rate_limit(request, db, "login")
    password_hash = get_admin_password_hash(request, db)
    if not password_hash:
        _record_failure(request, db, "login", rate_key, "password_not_set")
        raise HTTPException(status_code=400, detail="admin password is not set")
    if not verify_password_hash(str(payload.password or ""), password_hash):
        _record_failure(request, db, "login", rate_key, "invalid_password")
        raise HTTPException(status_code=401, detail="invalid password")

    _clear_rate_limit(request, rate_key)
    _audit(db, request, event_type="admin.login.success", outcome="success")
    cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
    cookie_value = mint_device_cookie_value(internal_secret=cookie_secret, password_hash=password_hash)
    body = AdminAuthStatusRead(password_set=True, trusted=True).model_dump(mode="json")
    response = JSONResponse(status_code=200, content=body)
    set_device_cookie(response, cookie_value, secure=secure_cookie(request))
    return response


def logout(request: Request, db: Session) -> JSONResponse:
    password_set = bool(get_admin_password_hash(request, db))
    body = AdminAuthStatusRead(password_set=password_set, trusted=False).model_dump(mode="json")
    response = JSONResponse(status_code=200, content=body)
    response.delete_cookie(key=DEVICE_COOKIE_NAME, path="/")
    return response
