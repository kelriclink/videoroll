from __future__ import annotations

from videoroll.apps.orchestrator_api.admin_auth_store import INTERNAL_TOKEN_HEADER
from videoroll.config import OrchestratorSettings
from videoroll.utils.internal_api_token import internal_api_token


def internal_header_token(settings: OrchestratorSettings) -> str:
    return internal_api_token(settings.s3_secret_access_key)


def admin_cookie_secret(settings: OrchestratorSettings) -> str:
    return internal_api_token(settings.s3_secret_access_key + ":admin-cookie")


def internal_http_headers(settings: OrchestratorSettings) -> dict[str, str]:
    token = internal_header_token(settings)
    return {INTERNAL_TOKEN_HEADER: token} if token else {}
