from __future__ import annotations

from urllib.parse import urlsplit

HTTPX_PROXY_KWARG_UNSUPPORTED = "The current backend httpx build does not support the `proxy` argument."
HTTPX_SOCKS_SUPPORT_MISSING = (
    "SOCKS proxy support is unavailable in the current backend environment. "
    "Install `httpx[socks]` (includes `socksio`) or use an `http://` proxy."
)


def uses_socks_proxy(proxy: str | None) -> bool:
    scheme = urlsplit(str(proxy or "").strip()).scheme.lower()
    return scheme.startswith("socks")


def format_httpx_proxy_error(error: Exception, *, proxy: str | None) -> str:
    message = str(error).strip() or type(error).__name__
    lower_message = message.lower()
    if uses_socks_proxy(proxy) and (
        "socksio" in lower_message
        or "httpx[socks]" in lower_message
        or "using socks proxy" in lower_message
    ):
        return HTTPX_SOCKS_SUPPORT_MISSING
    return message
