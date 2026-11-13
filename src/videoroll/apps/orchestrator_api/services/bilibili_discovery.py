"""Read-only Bilibili public ranking and activity feeds for the dashboard.

Never treat a public activity-list ID as a submission mission ID.
No login cookies, no publishing and no DB write. Each process caches successful
responses to cap upstream traffic; stale snapshots are marked on partial failure.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_RANK_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
_POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"
_ACTIVITY_URL = "https://api.bilibili.com/x/activity/page/list"
_TTL = 300
_FORCE_INTERVAL = 60
_MAX_STALE_AGE = 3600
_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] = {}
_BVID = re.compile(r"^BV[A-Za-z0-9]{10}$")


def _integer(value: object) -> int:
    try:
        result = int(value) if value is not None and not isinstance(value, bool) else 0
    except (ValueError, TypeError, OverflowError):
        return 0
    return max(0, result)


def _label(value: object, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _public_link(value: object, *, prefix: str = "/") -> str | None:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.hostname not in
            {"www.bilibili.com", "member.bilibili.com"} or not parsed.path.startswith(prefix)):
        return None
    return url


def _image_link(value: object) -> str | None:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".hdslb.com"):
        return None
    return url


def parse_ranking(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("code") != 0:
        raise ValueError("ranking API returned error code")
    rows = payload.get("data", {}).get("list") if isinstance(payload.get("data"), dict) else None
    if not isinstance(rows, list):
        raise ValueError("ranking payload has no list")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        bvid = _label(row.get("bvid"), 20)
        title = _label(row.get("title"), 120)
        if not _BVID.fullmatch(bvid) or not title or bvid in seen:
            continue
        seen.add(bvid)
        stats = row.get("stat") if isinstance(row.get("stat"), dict) else {}
        owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
        items.append({
            "rank": len(items) + 1,
            "bvid": bvid,
            "title": title,
            "category": _label(row.get("tname"), 40),
            "uploader": _label(owner.get("name"), 55),
            "cover": _image_link(row.get("pic")),
            "views": _integer(stats.get("view")),
            "likes": _integer(stats.get("like")),
            "danmaku": _integer(stats.get("danmaku")),
            "url": f"https://www.bilibili.com/video/{bvid}",
        })
        if len(items) >= 20:
            break
    return items


def parse_activities(payload: dict[str, Any], *, now: int) -> list[dict[str, Any]]:
    if payload.get("code") != 0:
        raise ValueError("activity API returned error code")
    rows = payload.get("data", {}).get("list") if isinstance(payload.get("data"), dict) else None
    if not isinstance(rows, list):
        raise ValueError("activity payload has no list")
    seen: set[int] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = _integer(row.get("id"))
        starts = _integer(row.get("stime"))
        ends = _integer(row.get("etime"))
        title = _label(row.get("name"), 110)
        link = _public_link(row.get("pc_url"), prefix="/blackboard/")
        if (not sid or sid in seen or not title or not link
                or row.get("state") not in (1, "1")
                or not starts or not ends or not (starts <= now <= ends)):
            continue
        try:
            start_at = datetime.fromtimestamp(starts, tz=timezone.utc).isoformat()
            end_at = datetime.fromtimestamp(ends, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        seen.add(sid)
        items.append({
            "id": sid,  # PUBLIC catalogue ID, NEVER topic_id/mission_id
            "title": title,
            "description": _label(row.get("desc"), 240),
            "start_at": start_at,
            "end_at": end_at,
            "cover": _image_link(row.get("pc_cover")),
            "url": link,
            "eligibility": "unknown",
        })
        if len(items) >= 30:
            break
    return items


def _upstream(url: str, params: dict[str, Any]) -> dict[str, Any]:
    # Fixed GET destinations; no upstream URL or credentials from the request.
    with httpx.Client(
        timeout=httpx.Timeout(8, connect=4),
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; VideoRollDashboard/1.0)",
                 "Referer": "https://www.bilibili.com/", "Accept": "application/json"},
    ) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected upstream JSON")
        return data


def _fetch_hot_videos() -> tuple[list[dict[str, Any]], str]:
    """Use official rank list if available, otherwise Bilibili's popular feed."""
    try:
        return parse_ranking(_upstream(_RANK_URL, {"rid": 0, "type": "all"})), "排行榜"
    except (httpx.HTTPError, ValueError, TypeError, OverflowError):
        # Ranking/v2 occasionally returns HTTP 200 + code=-352 even while
        # popular and activities APIs still work; label the fallback honestly.
        return parse_ranking(_upstream(_POPULAR_URL, {"pn": 1, "ps": 20})), "热门综合"


def _fetch_public_activities(*, now: int) -> list[dict[str, Any]]:
    """Read two bounded public catalogue pages; use page one if page two fails."""
    first = _upstream(_ACTIVITY_URL, {"plat": "1,3", "pn": 1, "ps": 30})
    # Validate first page before attempting any optional enrichment.
    primary = parse_activities(first, now=now)
    try:
        second = _upstream(_ACTIVITY_URL, {"plat": "1,3", "pn": 2, "ps": 30})
        if second.get("code") != 0 or not isinstance(second.get("data"), dict):
            return primary
        extra = second["data"].get("list")
        if not isinstance(extra, list):
            return primary
        return parse_activities({
            "code": 0,
            "data": {"list": first["data"]["list"] + extra},
        }, now=now)
    except (httpx.HTTPError, ValueError, TypeError, OverflowError):
        return primary


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _feed(name: str, *, refresh: bool, now: float) -> dict[str, Any]:
    cached = _CACHE.get(name)
    age = now - cached["timestamp"] if cached else None
    if cached and (age is not None and age < _TTL) and (not refresh or age < _FORCE_INTERVAL):
        return {"items": cached["items"], "fetched_at": _iso(cached["timestamp"]),
                "source": cached["source"], "stale": False, "error": None}
    try:
        if name == "ranking":
            data, source = _fetch_hot_videos()
        else:
            data, source = _fetch_public_activities(now=int(now)), "公开活动"
    except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
        logger.warning("Bilibili %s dashboard feed unavailable: %s", name, type(exc).__name__)
        if cached and age is not None and age < _MAX_STALE_AGE:
            return {"items": cached["items"], "fetched_at": _iso(cached["timestamp"]),
                    "source": cached["source"], "stale": True,
                    "error": "上游暂时不可用，显示缓存数据"}
        return {"items": [], "fetched_at": None, "source": None,
                "stale": False, "error": "B站数据暂时不可用"}
    _CACHE[name] = {"items": data, "timestamp": now, "source": source}
    return {"items": data, "fetched_at": _iso(now), "source": source,
            "stale": False, "error": None}


def get_bilibili_discovery(*, refresh: bool = False) -> dict[str, Any]:
    with _LOCK:
        now = time.time()
        return {
            "ranking": _feed("ranking", refresh=refresh, now=now),
            "activities": _feed("activities", refresh=refresh, now=now),
            "note": "公开活动列表仅供浏览，不代表账号有报名或投稿资格。",
        }
