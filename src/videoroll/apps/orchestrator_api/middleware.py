from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from videoroll.apps.orchestrator_api.admin_auth_store import (
    DEVICE_COOKIE_NAME,
    INTERNAL_TOKEN_HEADER,
    verify_device_cookie_value,
)
from videoroll.apps.orchestrator_api.remote_api_settings_store import REMOTE_AUTO_YOUTUBE_PATH
from videoroll.apps.orchestrator_api.services.auth_service import get_admin_password_hash


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        scope_path = str(request.scope.get("path") or getattr(request.url, "path", "") or "/")
        root_path = str(request.scope.get("root_path") or "").strip()
        path = scope_path
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :] or "/"
        if path == "/health" or path.endswith("/health"):
            return await call_next(request)
        if path.startswith("/auth"):
            return await call_next(request)
        if path.rstrip("/") == REMOTE_AUTO_YOUTUBE_PATH:
            return await call_next(request)

        password_hash = get_admin_password_hash(request)
        if not password_hash:
            return JSONResponse(status_code=403, content={"detail": "admin password not set"})

        internal_header_token = str(getattr(request.app.state, "internal_header_token", "") or "").strip()
        header_token = str(request.headers.get(INTERNAL_TOKEN_HEADER) or "").strip()
        if internal_header_token and header_token and hmac.compare_digest(header_token, internal_header_token):
            return await call_next(request)

        cookie_value = str(request.cookies.get(DEVICE_COOKIE_NAME) or "").strip()
        cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
        if cookie_value and cookie_secret and verify_device_cookie_value(
            cookie_value,
            internal_secret=cookie_secret,
            password_hash=password_hash,
        ):
            return await call_next(request)

        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
