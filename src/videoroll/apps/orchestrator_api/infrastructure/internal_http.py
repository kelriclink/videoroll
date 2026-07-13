from __future__ import annotations

from videoroll.apps.security.service_auth import (
    INTERNAL_TOKEN_HEADER,
    admin_cookie_secret as derive_admin_cookie_secret,
    service_token,
)
from videoroll.config import OrchestratorSettings


def internal_header_token(settings: OrchestratorSettings) -> str:
    return service_token(settings)


def admin_cookie_secret(settings: OrchestratorSettings) -> str:
    return derive_admin_cookie_secret(settings)


def internal_http_headers(settings: OrchestratorSettings) -> dict[str, str]:
    token = internal_header_token(settings)
    return {INTERNAL_TOKEN_HEADER: token} if token else {}
