from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{10,}$")
_YOUTUBE_PLAYLIST_ID_RE = re.compile(r"^(PL|UU|LL|FL|RD|OLAK5uy_)[A-Za-z0-9_-]{8,}$")


def _youtube_host(host: str | None) -> str:
    return str(host or "").strip().lower()


def _is_youtube_host(host: str) -> bool:
    return host == "youtu.be" or host.endswith(".youtu.be") or host == "youtube.com" or host.endswith(".youtube.com")


def _clean_video_id(value: str | None) -> Optional[str]:
    video_id = str(value or "").strip()
    if not video_id:
        return None
    return video_id if _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id) else None


def _clean_channel_id(value: str | None) -> Optional[str]:
    channel_id = str(value or "").strip()
    if not channel_id:
        return None
    return channel_id if _YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id) else None


def _clean_playlist_id(value: str | None) -> Optional[str]:
    playlist_id = str(value or "").strip()
    if not playlist_id:
        return None
    return playlist_id if _YOUTUBE_PLAYLIST_ID_RE.fullmatch(playlist_id) else None


def normalize_youtube_source_input(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}"
    lowered = raw.lower()
    if "://" not in raw and (lowered.startswith("youtube.com/") or lowered.startswith("www.youtube.com/") or lowered.startswith("m.youtube.com/")):
        return f"https://{raw}"
    return raw


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


def extract_youtube_playlist_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return None

    host = _youtube_host(parsed.hostname)
    if not _is_youtube_host(host):
        return None

    query = parse_qs(parsed.query)
    return _clean_playlist_id((query.get("list") or [None])[0])


def extract_youtube_channel_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return None

    host = _youtube_host(parsed.hostname)
    if not _is_youtube_host(host):
        return None

    query = parse_qs(parsed.query)
    direct = _clean_channel_id((query.get("channel_id") or [None])[0])
    if direct:
        return direct

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] == "channel":
        return _clean_channel_id(path_parts[1])

    return None


def extract_youtube_source_from_url(url: str) -> Optional[tuple[str, str]]:
    playlist_id = extract_youtube_playlist_id(url)
    if playlist_id:
        return ("playlist", playlist_id)

    channel_id = extract_youtube_channel_id(url)
    if channel_id:
        return ("channel", channel_id)

    return None


def canonicalize_youtube_source_url(source_type: str, source_id: str) -> str:
    source_type_clean = str(source_type or "").strip().lower()
    source_id_clean = str(source_id or "").strip()
    if source_type_clean == "channel":
        return f"https://www.youtube.com/channel/{source_id_clean}"
    if source_type_clean == "playlist":
        return f"https://www.youtube.com/playlist?list={source_id_clean}"
    return source_id_clean


def canonicalize_youtube_url(url: str) -> str:
    raw = str(url or "").strip()
    video_id = extract_youtube_video_id(raw)
    if not video_id:
        return raw
    return f"https://www.youtube.com/watch?v={video_id}"
