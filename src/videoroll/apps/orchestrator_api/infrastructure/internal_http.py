from __future__ import annotations

from dataclasses import dataclass

import httpx

from videoroll.apps.security.service_auth import (
    INTERNAL_TOKEN_HEADER,
    admin_cookie_secret as derive_admin_cookie_secret,
    service_token,
)
from videoroll.config import OrchestratorSettings


@dataclass(frozen=True)
class InternalServiceResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]


def internal_header_token(settings: OrchestratorSettings) -> str:
    return service_token(settings)


def admin_cookie_secret(settings: OrchestratorSettings) -> str:
    return derive_admin_cookie_secret(settings)


def internal_http_headers(settings: OrchestratorSettings) -> dict[str, str]:
    token = internal_header_token(settings)
    return {INTERNAL_TOKEN_HEADER: token} if token else {}


async def proxy_internal_service_request(
    settings: OrchestratorSettings,
    *,
    service_url: str,
    service_path: str,
    method: str,
    query_string: str,
    body: bytes,
    content_type: str | None,
) -> InternalServiceResponse:
    target_url = f"{service_url.rstrip('/')}/{service_path.lstrip('/')}"
    if query_string:
        target_url = f"{target_url}?{query_string}"
    headers = internal_http_headers(settings)
    if content_type:
        headers["content-type"] = content_type
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        response = await client.request(method.upper(), target_url, content=body)
    return InternalServiceResponse(
        status_code=response.status_code,
        content=response.content,
        headers={
            name: value
            for name in ("content-type", "content-disposition", "cache-control")
            if (value := response.headers.get(name))
        },
    )
