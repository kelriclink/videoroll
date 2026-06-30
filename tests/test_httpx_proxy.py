from __future__ import annotations

from unittest import TestCase

from videoroll.utils.httpx_proxy import (
    HTTPX_SOCKS_SUPPORT_MISSING,
    format_httpx_proxy_error,
    uses_socks_proxy,
)


class HttpxProxyUtilsTests(TestCase):
    def test_uses_socks_proxy_detects_socks_schemes(self) -> None:
        self.assertTrue(uses_socks_proxy("socks5://127.0.0.1:1080"))
        self.assertTrue(uses_socks_proxy("socks4://127.0.0.1:9050"))
        self.assertFalse(uses_socks_proxy("http://127.0.0.1:7890"))

    def test_format_httpx_proxy_error_rewrites_missing_socks_support(self) -> None:
        error = RuntimeError(
            "Using SOCKS proxy, but the 'socksio' package is not installed. "
            "Make sure to install httpx using `pip install httpx[socks]`."
        )

        self.assertEqual(
            format_httpx_proxy_error(error, proxy="socks5://127.0.0.1:1080"),
            HTTPX_SOCKS_SUPPORT_MISSING,
        )

    def test_format_httpx_proxy_error_keeps_other_errors(self) -> None:
        error = RuntimeError("connection timeout")

        self.assertEqual(
            format_httpx_proxy_error(error, proxy="http://127.0.0.1:7890"),
            "connection timeout",
        )
