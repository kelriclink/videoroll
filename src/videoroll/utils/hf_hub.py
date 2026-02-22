from __future__ import annotations

from typing import Callable

import os

import httpx


def configure_hf_hub_proxy(proxy: str | None) -> None:
    """
    Configure Hugging Face Hub's HTTP client.

    Note: `huggingface_hub.snapshot_download(..., proxies=...)` is ignored in newer
    versions; the recommended way is `huggingface_hub.set_client_factory`.
    """

    try:
        import huggingface_hub  # type: ignore
    except Exception:
        return

    set_factory = getattr(huggingface_hub, "set_client_factory", None)
    proxy = (proxy or "").strip()
    if not callable(set_factory):
        # Fallback for older huggingface_hub versions: rely on environment variables.
        if proxy:
            os.environ["HTTP_PROXY"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
            os.environ["http_proxy"] = proxy
            os.environ["https_proxy"] = proxy
        else:
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ.pop(k, None)
        return

    def _factory() -> httpx.Client:
        # trust_env=True keeps compatibility if the user configures proxy via env.
        # If `proxy` is provided, it overrides env proxies.
        if proxy:
            return httpx.Client(proxy=proxy, timeout=60.0, follow_redirects=True, trust_env=True)
        return httpx.Client(timeout=60.0, follow_redirects=True, trust_env=True)

    try:
        set_factory(_factory)  # type: ignore[misc]
    except Exception:
        # Best-effort: don't fail model downloads just because we can't hook HF client.
        return
