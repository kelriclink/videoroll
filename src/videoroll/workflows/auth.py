from __future__ import annotations

import hashlib
import hmac


INTERNAL_TOKEN_HEADER = "X-Videoroll-Internal-Token"
_SERVICE_TOKEN_CONTEXT = b"videoroll-internal-service:v1"
_KNOWN_DEFAULT_SECRETS = {
    "",
    "change-me",
    "changeme",
    "videoroll-development-internal-secret",
    "videoroll-development-bootstrap-secret",
}


def internal_service_token(secret: str, *, development_mode: bool) -> str:
    value = str(secret or "").strip()
    if not development_mode and value.lower() in _KNOWN_DEFAULT_SECRETS:
        raise ValueError("INTERNAL_API_SECRET must be set to a non-default value outside development mode")
    if not value:
        return ""
    return "v1." + hmac.new(value.encode("utf-8"), _SERVICE_TOKEN_CONTEXT, hashlib.sha256).hexdigest()
