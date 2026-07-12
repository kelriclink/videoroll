from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from videoroll.db.models import AppSetting
from videoroll.db.session import get_sessionmaker


INTERNAL_TOKEN_HEADER = "X-Videoroll-Internal-Token"
ADMIN_BOOTSTRAP_HEADER = "X-Videoroll-Admin-Bootstrap"
ADMIN_AUTH_SETTINGS_KEY = "admin.auth"

_SERVICE_TOKEN_CONTEXT = b"videoroll-internal-service:v1"
_ADMIN_COOKIE_CONTEXT = b"videoroll-admin-cookie:v1"
_KNOWN_DEFAULT_SECRETS = {
    "",
    "change-me",
    "changeme",
    "videoroll-development-internal-secret",
    "videoroll-development-bootstrap-secret",
}


def _secret_value(settings: Any, name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def _derive_secret(secret: str, context: bytes) -> str:
    return "v1." + hmac.new(secret.encode("utf-8"), context, hashlib.sha256).hexdigest()


def _validate_runtime_secret(settings: Any, name: str, env_name: str) -> str:
    value = _secret_value(settings, name)
    development_mode = bool(getattr(settings, "development_mode", True))
    if not development_mode and value.lower() in _KNOWN_DEFAULT_SECRETS:
        raise ValueError(f"{env_name} must be set to a non-default value outside development mode")
    return value


def service_token(settings: Any) -> str:
    secret = _validate_runtime_secret(settings, "internal_api_secret", "INTERNAL_API_SECRET")
    if not secret:
        return ""
    return _derive_secret(secret, _SERVICE_TOKEN_CONTEXT)


def admin_cookie_secret(settings: Any) -> str:
    secret = _validate_runtime_secret(settings, "internal_api_secret", "INTERNAL_API_SECRET")
    if not secret:
        return ""
    return _derive_secret(secret, _ADMIN_COOKIE_CONTEXT)


def validate_bootstrap_secret(settings: Any) -> None:
    _validate_runtime_secret(settings, "admin_bootstrap_secret", "ADMIN_BOOTSTRAP_SECRET")


def require_internal_service(request: Request) -> None:
    if request.url.path == "/health":
        return

    expected = str(getattr(request.app.state, "internal_service_token", "") or "").strip()
    presented = str(request.headers.get(INTERNAL_TOKEN_HEADER) or "").strip()
    if not presented:
        raise HTTPException(status_code=401, detail="internal service credentials required")
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="invalid internal service credentials")


class InternalServiceAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: FastAPI,
        settings_getter: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(app)
        self.settings_getter = settings_getter

    async def dispatch(self, request: Request, call_next):
        if not str(getattr(request.app.state, "internal_service_token", "") or "").strip():
            if self.settings_getter is not None:
                request.app.state.internal_service_token = service_token(self.settings_getter())
        try:
            require_internal_service(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        return await call_next(request)


def install_internal_service_auth(app: FastAPI, settings_getter: Callable[[], Any]) -> None:
    app.add_middleware(InternalServiceAuthMiddleware, settings_getter=settings_getter)


def _bootstrap_db(request: Request) -> tuple[Session, bool]:
    request_state = getattr(request, "state", None)
    existing = getattr(request_state, "bootstrap_db", None) if request_state is not None else None
    if isinstance(existing, Session):
        return existing, False

    database_url = str(getattr(request.app.state, "database_url", "") or "").strip()
    if not database_url:
        raise HTTPException(status_code=503, detail="bootstrap database unavailable")
    return get_sessionmaker(database_url)(), True


def ensure_bootstrap_state(db: Session) -> None:
    if db.get(AppSetting, ADMIN_AUTH_SETTINGS_KEY) is not None:
        return
    db.add(
        AppSetting(
            key=ADMIN_AUTH_SETTINGS_KEY,
            value_json={"bootstrap_consumed": False},
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def consume_bootstrap_secret(request: Request, presented: str) -> None:
    configured = str(getattr(request.app.state, "admin_bootstrap_secret", "") or "").strip()
    candidate = str(presented or "").strip()
    if not configured or not candidate or not hmac.compare_digest(candidate, configured):
        raise HTTPException(status_code=403, detail="invalid bootstrap secret")

    db, owns_session = _bootstrap_db(request)
    try:
        row = db.execute(
            select(AppSetting)
            .where(AppSetting.key == ADMIN_AUTH_SETTINGS_KEY)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            row = AppSetting(
                key=ADMIN_AUTH_SETTINGS_KEY,
                value_json={"bootstrap_consumed": False},
            )
            db.add(row)
            db.flush()
        else:
            db.refresh(row, attribute_names=["value_json", "version"])

        stored = dict(row.value_json) if isinstance(row.value_json, dict) else {}
        if bool(stored.get("bootstrap_consumed")):
            raise HTTPException(status_code=403, detail="bootstrap secret already consumed")
        stored["bootstrap_consumed"] = True
        row.value_json = stored
        row.version = max(1, int(row.version or 1)) + 1
        db.add(row)
        if owns_session:
            db.commit()
    except Exception:
        if owns_session:
            db.rollback()
        raise
    finally:
        if owns_session:
            db.close()
