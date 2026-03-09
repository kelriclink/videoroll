from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")


def _youtube_host(host: str | None) -> str:
    return str(host or "").strip().lower()


def _is_youtube_host(host: str) -> bool:
    return host == "youtu.be" or host.endswith(".youtu.be") or host == "youtube.com" or host.endswith(".youtube.com")


def _clean_video_id(value: str | None) -> Optional[str]:
    video_id = str(value or "").strip()
    if not video_id:
        return None
    return video_id if _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id) else None


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    return _is_youtube_host(_youtube_host(parsed.hostname))


def extract_youtube_video_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return None

    host = _youtube_host(parsed.hostname)
    if not _is_youtube_host(host):
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be" or host.endswith(".youtu.be"):
        return _clean_video_id(path_parts[0] if path_parts else None)

    query = parse_qs(parsed.query)
    direct_video_id = _clean_video_id((query.get("v") or [None])[0])
    if direct_video_id:
        return direct_video_id

    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
        return _clean_video_id(path_parts[1])

    return None


def canonicalize_youtube_url(url: str) -> str:
    raw = str(url or "").strip()
    video_id = extract_youtube_video_id(raw)
    if not video_id:
        return raw
    return f"https://www.youtube.com/watch?v={video_id}"
