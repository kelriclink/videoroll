from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx
import yt_dlp
from defusedxml.ElementTree import fromstring


logger = logging.getLogger(__name__)

MAX_FEED_FAILURE_REASON_LEN = 500
MAX_FEED_ERROR_LEN = 2000


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    title: str
    published_at: datetime | None


def _bounded_error_message(message: str, *, limit: int) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 1] + "…"


def _parse_datetime(value: str) -> datetime:
    # Expected: 2026-02-19T12:34:56+00:00 or Z
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def _parse_ytdlp_entry_datetime(entry: dict[str, Any]) -> datetime | None:
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

    return None


def _fetch_feed_ytdlp(
    source_type: str,
    source_id: str,
    user_agent: str,
    *,
    proxy: Optional[str] = None,
    limit: Optional[int] = None,
    channel_tab: str | None = None,
) -> list[FeedEntry]:
    if source_type == "channel":
        tab = str(channel_tab or "videos").strip().lower()
        if tab not in {"videos", "shorts"}:
            raise ValueError("invalid YouTube channel tab")
        url = f"https://www.youtube.com/channel/{source_id}/{tab}"
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
        raise RuntimeError("yt-dlp did not return a source listing")
    if not isinstance(entries, list):
        entries = list(entries)

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


def _merge_channel_entries(*entry_sets: Iterable[FeedEntry]) -> list[FeedEntry]:
    """Sort known dates newest first, keeping undated entries in discovery order."""
    merged: list[FeedEntry] = []
    seen_video_ids: set[str] = set()
    for entries in entry_sets:
        for entry in entries:
            video_id = str(entry.video_id or "").strip()
            if not video_id or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            merged.append(entry)
    dated_entries = sorted(
        (entry for entry in merged if entry.published_at is not None),
        key=lambda entry: entry.published_at,
        reverse=True,
    )
    return dated_entries + [entry for entry in merged if entry.published_at is None]


def _fetch_channel_uploads_ytdlp(
    source_id: str,
    user_agent: str,
    *,
    proxy: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[FeedEntry]:
    """Enumerate both public channel upload tabs without letting one hide the other."""
    feeds: list[list[FeedEntry]] = []
    failures: list[str] = []
    for tab in ("videos", "shorts"):
        try:
            feeds.append(
                _fetch_feed_ytdlp(
                    "channel",
                    source_id,
                    user_agent,
                    proxy=proxy,
                    limit=limit,
                    channel_tab=tab,
                )
            )
        except Exception as exc:
            reason = _bounded_error_message(str(exc), limit=MAX_FEED_FAILURE_REASON_LEN)
            failures.append(f"{tab}: {reason}")
    combined = _merge_channel_entries(*feeds)
    if limit is not None:
        try:
            combined = combined[: max(1, int(limit))]
        except Exception:
            pass
    if failures:
        message = _bounded_error_message("; ".join(failures), limit=MAX_FEED_ERROR_LEN)
        if not feeds:
            raise RuntimeError(message)
        warning = f"YouTube channel {source_id} scan was partial ({message})"
        logger.warning("%s", _bounded_error_message(warning, limit=MAX_FEED_ERROR_LEN))
    return combined


def _fetch_feed_rss(
    source_type: str,
    source_id: str,
    user_agent: str,
    timeout_s: float = 20.0,
    *,
    proxy: Optional[str] = None,
) -> list[FeedEntry]:
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
        client_kwargs["proxy"] = proxy

    # 2025+ YouTube RSS feeds are intermittently unavailable (often 404).
    # Let the caller distinguish these failures from a valid, empty Atom feed.
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

    root = fromstring(text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    if root.tag != f"{{{ns['atom']}}}feed":
        raise ValueError("YouTube RSS response is not an Atom feed")
    out: list[FeedEntry] = []
    for entry in root.findall("atom:entry", ns):
        video_id_el = entry.find("yt:videoId", ns)
        title_el = entry.find("atom:title", ns)
        published_el = entry.find("atom:published", ns)
        if video_id_el is None or title_el is None or published_el is None:
            continue
        out.append(
            FeedEntry(
                video_id=(video_id_el.text or "").strip(),
                title=(title_el.text or "").strip(),
                published_at=_parse_datetime((published_el.text or "").strip()),
            )
        )
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
    # YouTube's RSS feed exposes only the most recent 15 uploads and does not
    # reliably expose the full Shorts tab. Channels therefore use yt-dlp first
    # and merge their Videos and Shorts tabs; RSS remains an outage fallback.
    attempts: list[tuple[str, Any]]
    if source_type == "channel":
        attempts = [
            ("yt-dlp", lambda: _fetch_channel_uploads_ytdlp(source_id, user_agent, proxy=proxy, limit=limit)),
            ("rss", lambda: _fetch_feed_rss(source_type, source_id, user_agent, timeout_s, proxy=proxy)),
        ]
    else:
        # Playlists may be represented by RSS, and do not have a separate Shorts tab.
        attempts = [
            ("yt-dlp", lambda: _fetch_feed_ytdlp(source_type, source_id, user_agent, proxy=proxy, limit=limit)),
            ("rss", lambda: _fetch_feed_rss(source_type, source_id, user_agent, timeout_s, proxy=proxy)),
        ]
        if limit is not None:
            try:
                if int(limit) <= 15:
                    attempts.reverse()
            except (TypeError, ValueError):
                pass

    failures: list[str] = []
    successful_fetch = False
    for name, loader in attempts:
        try:
            entries = loader()
        except Exception as exc:
            reason = _bounded_error_message(str(exc), limit=MAX_FEED_FAILURE_REASON_LEN)
            failures.append(f"{name}: {reason}")
            continue
        successful_fetch = True
        if entries:
            for entry in entries:
                yield entry
            return
    if not successful_fetch:
        message = "all YouTube feed fetch attempts failed: " + "; ".join(failures)
        raise RuntimeError(_bounded_error_message(message, limit=MAX_FEED_ERROR_LEN))
