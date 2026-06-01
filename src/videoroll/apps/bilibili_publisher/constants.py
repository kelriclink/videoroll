from __future__ import annotations


BILIBILI_DESC_PLATFORM_MAX_CHARS = 2000
# Keep a margin below Bilibili's documented limit because the web API can count
# rich text, wide characters, and line breaks more strictly than plain len().
BILIBILI_DESC_MAX_CHARS = 1800
BILIBILI_DESC_RETRY_MAX_CHARS = 1600
