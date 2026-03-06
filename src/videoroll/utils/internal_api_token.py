from __future__ import annotations

import hashlib


_SALT = "videoroll-internal-token:v1:"


def internal_api_token(secret: str) -> str:
    secret = str(secret or "").strip()
    if not secret:
        return ""
    h = hashlib.sha256()
    h.update((_SALT + secret).encode("utf-8"))
    return h.hexdigest()

