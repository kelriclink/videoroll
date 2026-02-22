from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx
import yt_dlp
from defusedxml.ElementTree import fromstring


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    title: str
    published_at: datetime


def _parse_datetime(value: str) -> datetime:
    # Expected: 2026-02-19T12:34:56+00:00 or Z
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_ytdlp_entry_datetime(entry: dict[str, Any]) -> datetime:
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts is not None:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except Exception:
            pass

    upload_date = entry.get("upload_date") or entry.get("release_date")
    if isinstance(upload_date, str) and upload_date.strip():
        s = upload_date.strip()
        try:
            if len(s) == 8 and s.isdigit():
                # YYYYMMDD
                return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]), tzinfo=timezone.utc)
        except Exception:
            pass

    return _utcnow()


def _fetch_feed_ytdlp(
    source_type: str,
    source_id: str,
    user_agent: str,
    *,
    proxy: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[FeedEntry]:
    if source_type == "channel":
        url = f"https://www.youtube.com/channel/{source_id}/videos"
    elif source_type == "playlist":
        url = f"https://www.youtube.com/playlist?list={source_id}"
    else:
        raise ValueError("invalid source_type")

    proxy = (proxy or "").strip() or None
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "retries": 3,
        "fragment_retries": 3,
        "http_headers": {"User-Agent": user_agent},
    }
    if proxy:
        opts["proxy"] = proxy
    if limit is not None:
        try:
            opts["playlistend"] = max(1, int(limit))
        except Exception:
            pass

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    d = info if isinstance(info, dict) else {}
    entries = d.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        try:
            entries = list(entries)  # type: ignore[arg-type]
        except Exception:
            return []

    out: list[FeedEntry] = []
    for item in entries:
        e = item if isinstance(item, dict) else {}
        vid = str(e.get("id") or "").strip()
        if not vid:
            # yt-dlp may provide an url like "https://www.youtube.com/watch?v=..."
            u = e.get("url")
            if isinstance(u, str) and "v=" in u:
                try:
                    vid = u.split("v=", 1)[1].split("&", 1)[0].strip()
                except Exception:
                    vid = ""
        if not vid:
            continue
        title = str(e.get("title") or "").strip()
        out.append(FeedEntry(video_id=vid, title=title, published_at=_parse_ytdlp_entry_datetime(e)))
    return out


def fetch_youtube_feed(
    source_type: str,
    source_id: str,
    user_agent: str,
    timeout_s: float = 20.0,
    *,
    proxy: Optional[str] = None,
    limit: Optional[int] = None,
) -> Iterable[FeedEntry]:
    if source_type == "channel":
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={source_id}"
    elif source_type == "playlist":
        url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={source_id}"
    else:
        raise ValueError("invalid source_type")

    headers = {"User-Agent": user_agent}

    proxy = (proxy or "").strip() or None
    client_kwargs: dict[str, Any] = {"timeout": timeout_s, "headers": headers, "follow_redirects": True}
    if proxy:
        try:
            client_kwargs["proxy"] = proxy
        except Exception:
            pass

    # 2025+ YouTube RSS feeds are intermittently unavailable (often 404).
    # Prefer RSS when it works (fast), but fall back to yt-dlp extraction when it doesn't.
    text: Optional[str] = None
    try:
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.get(url)
                resp.raise_for_status()
                text = resp.text
        except TypeError:
            with httpx.Client(timeout=timeout_s, headers=headers, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                text = resp.text
    except Exception:
        text = None

    if text:
        try:
            root = fromstring(text)
            ns = {
                "atom": "http://www.w3.org/2005/Atom",
                "yt": "http://www.youtube.com/xml/schemas/2015",
            }
            for entry in root.findall("atom:entry", ns):
                video_id_el = entry.find("yt:videoId", ns)
                title_el = entry.find("atom:title", ns)
                published_el = entry.find("atom:published", ns)
                if video_id_el is None or title_el is None or published_el is None:
                    continue
                yield FeedEntry(
                    video_id=(video_id_el.text or "").strip(),
                    title=(title_el.text or "").strip(),
                    published_at=_parse_datetime((published_el.text or "").strip()),
                )
            return
        except Exception:
            # Fall back to yt-dlp.
            pass

    for e in _fetch_feed_ytdlp(source_type, source_id, user_agent, proxy=proxy, limit=limit):
        yield e
