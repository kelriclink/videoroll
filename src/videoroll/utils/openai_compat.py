from __future__ import annotations

from urllib.parse import urlparse, urlunparse


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def _ensure_scheme(url: str, default_scheme: str = "https") -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        return f"{default_scheme}://{u}"
    return u


def normalize_openai_base_url(base_url: str) -> str:
    """
    Normalize an OpenAI-compatible base URL.

    - Ensures scheme (defaults to https://)
    - If the URL has no path (or just "/"), appends "/v1"
    - Strips trailing slash
    """
    raw = (base_url or "").strip()
    if not raw:
        return DEFAULT_OPENAI_BASE_URL

    raw = _ensure_scheme(raw)
    p = urlparse(raw)
    path = p.path or ""
    if path in {"", "/"}:
        path = "/v1"
    path = path.rstrip("/")

    normalized = urlunparse(p._replace(path=path))
    return normalized.rstrip("/")


def build_openai_chat_completions_url(base_url: str) -> str:
    """
    Accepts either:
    - a base URL (e.g. https://api.openai.com/v1), or
    - a full endpoint URL ending with /chat/completions.
    """
    raw = (base_url or "").strip()
    if not raw:
        raw = DEFAULT_OPENAI_BASE_URL
    raw = _ensure_scheme(raw).rstrip("/")

    if raw.endswith("/chat/completions"):
        return raw

    base = normalize_openai_base_url(raw)
    return base.rstrip("/") + "/chat/completions"

