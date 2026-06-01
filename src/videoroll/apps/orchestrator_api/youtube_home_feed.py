from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import random
import re
import string
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


YOUTUBE_ORIGIN = "https://www.youtube.com"
YOUTUBE_HOME_URL = f"{YOUTUBE_ORIGIN}/"
YOUTUBE_BROWSE_URL = f"{YOUTUBE_ORIGIN}/youtubei/v1/browse"
YOUTUBE_SW_DATA_URL = f"{YOUTUBE_ORIGIN}/sw.js_data"
YOUTUBE_WEB_CLIENT_NAME = "WEB"
YOUTUBE_WEB_CLIENT_NAME_ID = "1"
YOUTUBE_WEB_API_VERSION = "v1"
YOUTUBE_WEB_DEFAULT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
YOUTUBE_WEB_DEFAULT_CLIENT_VERSION = "2.20260206.01.00"
YOUTUBE_HOME_BROWSE_ID = "FEwhat_to_watch"
YOUTUBE_LONG_VIDEO_MIN_DURATION_SECONDS = 180
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_DURATION_COLON_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
_DURATION_WORD_RE = re.compile(
    r"(?P<num>\d+)\s*(?P<unit>hours?|hrs?|hr|minutes?|mins?|min|seconds?|secs?|sec)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class HomeFeedVideo:
    video_id: str
    title: str
    url: str
    renderer_type: str | None = None
    is_short: bool = False
    duration_seconds: int | None = None
    duration_source: str | None = None
    short_reason: str | None = None


@dataclass(frozen=True)
class HomeFeedFetchStats:
    requested_limit: int
    candidate_limit: int
    long_videos_only: bool = False
    min_duration_seconds: int = YOUTUBE_LONG_VIDEO_MIN_DURATION_SECONDS
    candidate_count: int = 0
    explicit_shorts_count: int = 0
    known_duration_count: int = 0
    unknown_duration_count: int = 0
    below_min_duration_count: int = 0
    kept_unknown_duration_count: int = 0
    eligible_count: int = 0
    returned_count: int = 0
    log_lines: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class HomeFeedFetchResult:
    videos: list[HomeFeedVideo]
    stats: HomeFeedFetchStats


def _cookie_header_from_netscape(cookies_txt: str, *, url: str = YOUTUBE_HOME_URL) -> str:
    from videoroll.apps.youtube_settings_store import normalize_and_validate_netscape_cookies_txt

    normalized = normalize_and_validate_netscape_cookies_txt(cookies_txt)
    jar = http.cookiejar.MozillaCookieJar()
    jar._really_load(io.StringIO(normalized), "<youtube_cookies>", ignore_discard=True, ignore_expires=True)
    req = urllib.request.Request(url)
    jar.add_cookie_header(req)
    return str(req.get_header("Cookie") or "").strip()


def _get_cookie(cookie_header: str, name: str) -> Optional[str]:
    name = str(name or "").strip()
    if not name:
        return None
    prefix = f"{name}="
    for part in str(cookie_header or "").split(";"):
        token = part.strip()
        if token.startswith(prefix):
            value = token[len(prefix) :].strip()
            return value or None
    return None


def _generate_sid_auth(sid: str) -> str:
    ts = int(time.time())
    raw = f"{ts} {sid} {YOUTUBE_ORIGIN}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _random_visitor_id(length: int = 11) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(alphabet) for _ in range(max(1, int(length or 11))))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _list_get(value: Any, index: int, default: Any = None) -> Any:
    if isinstance(value, list) and 0 <= index < len(value):
        return value[index]
    return default


def _text_from_runs(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    simple = value.get("simpleText")
    if isinstance(simple, str) and simple.strip():
        return simple.strip()
    runs = value.get("runs")
    if isinstance(runs, list):
        parts: list[str] = []
        for item in runs:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def _extract_title(node: dict[str, Any]) -> str:
    for key in ("title", "headline", "primaryText", "secondaryText"):
        text = _text_from_runs(node.get(key))
        if text:
            return text
    for path in (
        ("metadata", "lockupMetadataViewModel", "title"),
        ("overlayMetadata", "primaryText"),
        ("overlayMetadata", "secondaryText"),
    ):
        text = _text_from_runs(_path_get(node, *path))
        if text:
            return text
    overlays = node.get("thumbnailOverlays")
    if isinstance(overlays, list):
        for item in overlays:
            if isinstance(item, dict):
                title = _extract_title(item)
                if title:
                    return title
    return ""


def _duration_text_to_seconds(text: str) -> int | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if not parts or len(parts) > 3:
        return None
    try:
        nums = [int(part) for part in parts]
    except Exception:
        return None
    if len(nums) == 3:
        hours, minutes, seconds = nums
        return hours * 3600 + minutes * 60 + seconds
    if len(nums) == 2:
        minutes, seconds = nums
        return minutes * 60 + seconds
    return nums[0]


def _duration_label_to_seconds(text: str) -> int | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    colon_match = _DURATION_COLON_RE.search(raw)
    if colon_match:
        suffix = raw[colon_match.end() : colon_match.end() + 8].lower()
        if "ago" in suffix:
            return None
        seconds = _duration_text_to_seconds(colon_match.group(1))
        if seconds is not None:
            return seconds

    total = 0
    matched = False
    last_end = -1
    for match in _DURATION_WORD_RE.finditer(raw):
        matched = True
        last_end = max(last_end, int(match.end()))
        try:
            num = int(match.group("num"))
        except Exception:
            continue
        unit = str(match.group("unit") or "").lower()
        if unit.startswith("hour") or unit.startswith("hr"):
            total += num * 3600
        elif unit.startswith("min"):
            total += num * 60
        elif unit.startswith("sec"):
            total += num
    if matched and last_end >= 0:
        suffix = raw[last_end : last_end + 8].lower()
        if "ago" in suffix:
            return None
    return total if matched and total > 0 else None


def _collect_label_like_texts(node: Any, out: list[str], *, depth: int = 0) -> None:
    if depth > 8 or len(out) >= 40:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                key_name = str(key or "").strip().lower()
                if key_name in {"label", "accessibilitylabel", "tooltip"}:
                    text = value.strip()
                    if text and text not in out:
                        out.append(text)
            elif isinstance(value, dict):
                if "simpleText" in value or "runs" in value:
                    text = _text_from_runs(value)
                    if text:
                        key_name = str(key or "").strip().lower()
                        if key_name in {"lengthtext", "thumbnailtext", "text", "title", "headline"} and text not in out:
                            out.append(text)
                _collect_label_like_texts(value, out, depth=depth + 1)
            elif isinstance(value, list):
                _collect_label_like_texts(value, out, depth=depth + 1)
        return
    if isinstance(node, list):
        for item in node:
            _collect_label_like_texts(item, out, depth=depth + 1)


def _extract_duration_seconds(node: dict[str, Any]) -> tuple[int | None, str | None]:
    for key in ("lengthText", "thumbnailText"):
        seconds = _duration_text_to_seconds(_text_from_runs(node.get(key)))
        if seconds is not None:
            return seconds, key
    for item in _iter_overlay_items(node):
        overlay = item.get("thumbnailOverlayTimeStatusRenderer")
        if isinstance(overlay, dict):
            seconds = _duration_text_to_seconds(_text_from_runs(overlay.get("text")))
            if seconds is not None:
                return seconds, "thumbnailOverlayTimeStatusRenderer.text"
        for text in _overlay_badge_texts(item):
            seconds = _duration_text_to_seconds(text) or _duration_label_to_seconds(text)
            if seconds is not None:
                return seconds, "thumbnailOverlayBadgeViewModel.text"

    text_candidates: list[str] = []
    _collect_label_like_texts(node, text_candidates)
    for text in text_candidates:
        seconds = _duration_label_to_seconds(text)
        if seconds is not None:
            return seconds, "label"
    return None, None


def _path_get(node: dict[str, Any], *keys: str) -> Any:
    cur: Any = node
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _format_duration_seconds(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _extract_shorts_lockup_video_id(node: dict[str, Any]) -> str:
    candidates = [
        _path_get(node, "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId"),
        _path_get(node, "navigationEndpoint", "reelWatchEndpoint", "videoId"),
        _path_get(node, "overlayMetadata", "primaryText", "content"),
    ]
    for value in candidates:
        vid = str(value or "").strip()
        if _VIDEO_ID_RE.match(vid):
            return vid
    return ""


def _extract_lockup_video_id(node: dict[str, Any]) -> str:
    content_type = str(node.get("contentType") or "").strip().upper()
    if content_type and content_type != "LOCKUP_CONTENT_TYPE_VIDEO":
        return ""
    candidates = [
        node.get("contentId"),
        _path_get(node, "rendererContext", "commandContext", "onTap", "innertubeCommand", "watchEndpoint", "videoId"),
        _path_get(node, "rendererContext", "commandContext", "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId"),
    ]
    for value in candidates:
        vid = str(value or "").strip()
        if _VIDEO_ID_RE.match(vid):
            return vid
    return ""


def _extract_video_id(node: dict[str, Any], renderer_type: str | None) -> str:
    if renderer_type == "shortsLockupViewModel":
        return _extract_shorts_lockup_video_id(node)
    if renderer_type == "lockupViewModel":
        return _extract_lockup_video_id(node)
    candidates = [
        node.get("videoId"),
        _path_get(node, "navigationEndpoint", "watchEndpoint", "videoId"),
        _path_get(node, "onTap", "innertubeCommand", "watchEndpoint", "videoId"),
        _path_get(node, "videoCommand", "watchEndpoint", "videoId"),
    ]
    for value in candidates:
        vid = str(value or "").strip()
        if _VIDEO_ID_RE.match(vid):
            return vid
    return ""


def _iter_overlay_items(node: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    containers = [
        node.get("thumbnailOverlays"),
        _path_get(node, "contentImage", "thumbnailViewModel", "overlays"),
        _path_get(node, "contentImage", "collectionThumbnailViewModel", "primaryThumbnail", "thumbnailViewModel", "overlays"),
    ]
    for container in containers:
        if not isinstance(container, list):
            continue
        for item in container:
            if isinstance(item, dict):
                items.append(item)
    return items


def _overlay_badge_texts(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    candidates = []
    badge_view = item.get("thumbnailOverlayBadgeViewModel")
    if isinstance(badge_view, dict):
        badges = badge_view.get("thumbnailBadges")
        if isinstance(badges, list):
            candidates.extend(badges)
        candidates.append(badge_view)
    if isinstance(item.get("thumbnailBadgeViewModel"), dict):
        candidates.append(item.get("thumbnailBadgeViewModel"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        badge_model = candidate.get("thumbnailBadgeViewModel") if isinstance(candidate.get("thumbnailBadgeViewModel"), dict) else candidate
        if not isinstance(badge_model, dict):
            continue
        for key in ("text", "label"):
            text = _text_from_runs(badge_model.get(key)) if isinstance(badge_model.get(key), dict) else str(badge_model.get(key) or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def _detect_short_reason(node: dict[str, Any], renderer_type: str | None) -> str | None:
    if renderer_type in {"reelItemRenderer", "shortsLockupViewModel"}:
        return renderer_type
    if _path_get(node, "navigationEndpoint", "reelWatchEndpoint", "videoId"):
        return "navigationEndpoint.reelWatchEndpoint"
    if _path_get(node, "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId"):
        return "onTap.innertubeCommand.reelWatchEndpoint"
    if _path_get(node, "rendererContext", "commandContext", "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId"):
        return "rendererContext.commandContext.onTap.innertubeCommand.reelWatchEndpoint"
    for item in _iter_overlay_items(node):
        overlay = item.get("thumbnailOverlayTimeStatusRenderer")
        if isinstance(overlay, dict):
            style = str(overlay.get("style") or "").strip().upper()
            if style == "SHORTS":
                return "thumbnailOverlayTimeStatusRenderer.style"
        for text in _overlay_badge_texts(item):
            if "SHORTS" in text.upper():
                return "thumbnailOverlayBadgeViewModel.text"
    icon_type = str(_path_get(node, "navigationEndpoint", "commandMetadata", "webCommandMetadata", "rootVe") or "").strip()
    if "SHORTS" in icon_type.upper():
        return "webCommandMetadata.rootVe"
    for value in (
        _path_get(node, "navigationEndpoint", "commandMetadata", "webCommandMetadata", "url"),
        _path_get(node, "onTap", "innertubeCommand", "commandMetadata", "webCommandMetadata", "url"),
        _path_get(node, "rendererContext", "commandContext", "onTap", "innertubeCommand", "commandMetadata", "webCommandMetadata", "url"),
    ):
        url = str(value or "").strip()
        if "/shorts/" in url:
            return "webCommandMetadata.url"
    return None


def _looks_like_video_node(node: dict[str, Any], renderer_type: str | None) -> bool:
    video_id = _extract_video_id(node, renderer_type)
    if not _VIDEO_ID_RE.match(video_id):
        return False
    if node.get("playlistId"):
        return False
    if _extract_title(node):
        return True
    return "thumbnail" in node or "thumbnails" in node or "contentImage" in node


def _extract_video(node: dict[str, Any], renderer_type: str | None) -> HomeFeedVideo | None:
    if not _looks_like_video_node(node, renderer_type):
        return None
    video_id = _extract_video_id(node, renderer_type)
    if not video_id:
        return None
    duration_seconds, duration_source = _extract_duration_seconds(node)
    short_reason = _detect_short_reason(node, renderer_type)
    is_short = bool(short_reason)
    return HomeFeedVideo(
        video_id=video_id,
        title=_extract_title(node),
        url=f"{YOUTUBE_ORIGIN}/watch?v={video_id}",
        renderer_type=renderer_type,
        is_short=is_short,
        duration_seconds=duration_seconds,
        duration_source=duration_source,
        short_reason=short_reason,
    )


def _collect_videos(
    node: Any,
    out: list[HomeFeedVideo],
    seen_ids: set[str],
    *,
    limit: Optional[int] = None,
    renderer_type: str | None = None,
) -> None:
    if limit is not None and len(out) >= limit:
        return
    if isinstance(node, dict):
        item = _extract_video(node, renderer_type)
        if item is not None and item.video_id not in seen_ids:
            seen_ids.add(item.video_id)
            out.append(item)
            if limit is not None and len(out) >= limit:
                return
        for key, value in node.items():
            child_renderer_type = key if isinstance(key, str) else None
            _collect_videos(value, out, seen_ids, limit=limit, renderer_type=child_renderer_type)
            if limit is not None and len(out) >= limit:
                return
        return
    if isinstance(node, list):
        for item in node:
            _collect_videos(item, out, seen_ids, limit=limit, renderer_type=renderer_type)
            if limit is not None and len(out) >= limit:
                return


def _build_log_line(item: HomeFeedVideo, *, decision: str, detail: str) -> str:
    title = str(item.title or "").strip() or item.video_id
    title = title.replace("\n", " ").strip()
    if len(title) > 72:
        title = title[:71] + "…"
    duration = _format_duration_seconds(item.duration_seconds)
    renderer = str(item.renderer_type or "-")
    return f"[{decision}] {item.video_id} dur={duration} type={renderer} {detail} :: {title}"


def _filter_long_videos(
    videos: list[HomeFeedVideo],
    *,
    min_duration_seconds: int,
    requested_limit: int,
    candidate_limit: int,
) -> tuple[list[HomeFeedVideo], HomeFeedFetchStats]:
    eligible: list[HomeFeedVideo] = []
    log_lines: list[str] = []
    explicit_shorts_count = 0
    known_duration_count = 0
    unknown_duration_count = 0
    below_min_duration_count = 0
    kept_unknown_duration_count = 0
    min_duration = max(0, int(min_duration_seconds or 0))

    for item in videos:
        if item.is_short:
            explicit_shorts_count += 1
            if len(log_lines) < 20:
                log_lines.append(_build_log_line(item, decision="skip", detail=f"short={item.short_reason or 'explicit'}"))
            continue

        duration = item.duration_seconds
        if duration is None:
            unknown_duration_count += 1
            kept_unknown_duration_count += 1
            eligible.append(item)
            if len(log_lines) < 20:
                if min_duration > 0:
                    log_lines.append(_build_log_line(item, decision="keep", detail="duration=unknown non-shorts kept"))
                else:
                    log_lines.append(_build_log_line(item, decision="keep", detail="duration filter disabled"))
            continue

        known_duration_count += 1
        if min_duration > 0 and duration < min_duration:
            below_min_duration_count += 1
            if len(log_lines) < 20:
                log_lines.append(_build_log_line(item, decision="skip", detail=f"duration<{min_duration}s"))
            continue

        eligible.append(item)
        if len(log_lines) < 20:
            detail = f"duration>={min_duration}s" if min_duration > 0 else "duration accepted"
            log_lines.append(_build_log_line(item, decision="keep", detail=detail))

    returned = eligible[:requested_limit]
    summary_lines = [
        f"raw_candidates={len(videos)} candidate_limit={candidate_limit} requested={requested_limit}",
        f"explicit_shorts={explicit_shorts_count} known_duration={known_duration_count} unknown_duration={unknown_duration_count}",
        f"below_min_duration={below_min_duration_count} kept_unknown_duration={kept_unknown_duration_count} eligible={len(eligible)} returned={len(returned)}",
        f"min_duration_seconds={min_duration}",
    ]
    stats = HomeFeedFetchStats(
        requested_limit=requested_limit,
        candidate_limit=candidate_limit,
        long_videos_only=True,
        min_duration_seconds=min_duration,
        candidate_count=len(videos),
        explicit_shorts_count=explicit_shorts_count,
        known_duration_count=known_duration_count,
        unknown_duration_count=unknown_duration_count,
        below_min_duration_count=below_min_duration_count,
        kept_unknown_duration_count=kept_unknown_duration_count,
        eligible_count=len(eligible),
        returned_count=len(returned),
        log_lines=tuple(summary_lines + log_lines),
    )
    return returned, stats


def extract_home_feed_videos(
    payload: dict[str, Any],
    *,
    limit: Optional[int] = None,
    long_videos_only: bool = False,
    min_duration_seconds: int = YOUTUBE_LONG_VIDEO_MIN_DURATION_SECONDS,
) -> list[HomeFeedVideo]:
    out: list[HomeFeedVideo] = []
    _collect_videos(payload, out, set(), limit=limit)
    if long_videos_only:
        filtered, _stats = _filter_long_videos(
            out,
            min_duration_seconds=min_duration_seconds,
            requested_limit=limit if limit is not None else len(out),
            candidate_limit=limit if limit is not None else len(out),
        )
        out = filtered
    return out[:limit] if limit is not None else out


def _collect_continuations(node: Any, out: list[str], seen_tokens: set[str]) -> None:
    if isinstance(node, dict):
        for key in ("continuationCommand", "nextContinuationData", "reloadContinuationData"):
            raw = node.get(key)
            if isinstance(raw, dict):
                token = str(raw.get("token") or raw.get("continuation") or "").strip()
                if token and token not in seen_tokens:
                    seen_tokens.add(token)
                    out.append(token)
        for value in node.values():
            _collect_continuations(value, out, seen_tokens)
        return
    if isinstance(node, list):
        for item in node:
            _collect_continuations(item, out, seen_tokens)


def extract_home_feed_continuations(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    _collect_continuations(payload, out, set())
    return out


def _build_context(
    *,
    visitor_data: str,
    client_version: str,
    user_agent: str,
    os_name: str,
    os_version: str,
    browser_name: str,
    browser_version: str,
    device_make: str,
    device_model: str,
    timezone_name: str,
    rollout_token: Optional[str] = None,
    device_experiment_id: Optional[str] = None,
    app_install_data: Optional[str] = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "client": {
            "hl": "en",
            "gl": "US",
            "remoteHost": "",
            "screenDensityFloat": 1,
            "screenHeightPoints": 1440,
            "screenPixelDensity": 1,
            "screenWidthPoints": 2560,
            "visitorData": visitor_data,
            "clientName": YOUTUBE_WEB_CLIENT_NAME,
            "clientVersion": client_version,
            "osName": os_name,
            "osVersion": os_version,
            "userAgent": user_agent,
            "platform": "DESKTOP",
            "clientFormFactor": "UNKNOWN_FORM_FACTOR",
            "userInterfaceTheme": "USER_INTERFACE_THEME_LIGHT",
            "timeZone": timezone_name,
            "originalUrl": YOUTUBE_HOME_URL,
            "deviceMake": device_make,
            "deviceModel": device_model,
            "browserName": browser_name,
            "browserVersion": browser_version,
            "utcOffsetMinutes": -time.timezone // 60,
            "memoryTotalKbytes": "8000000",
            "mainAppWebInfo": {
                "graftUrl": YOUTUBE_HOME_URL,
                "pwaInstallabilityStatus": "PWA_INSTALLABILITY_STATUS_UNKNOWN",
                "webDisplayMode": "WEB_DISPLAY_MODE_BROWSER",
                "isWebNativeShareAvailable": True,
            },
        },
        "user": {
            "enableSafetyMode": False,
            "lockedSafetyMode": False,
        },
        "request": {
            "useSsl": True,
            "internalExperimentFlags": [],
        },
    }
    if rollout_token:
        context["client"]["rolloutToken"] = rollout_token
    if device_experiment_id:
        context["client"]["deviceExperimentId"] = device_experiment_id
    if app_install_data:
        context["client"]["configInfo"] = {"appInstallData": app_install_data}
    return context


def parse_sw_session_data(
    text: str,
    *,
    user_agent: str,
    timezone_name: str = "UTC",
    visitor_cookie: str = "",
) -> dict[str, Any]:
    raw = str(text or "")
    if not raw.startswith(")]}'"):
        raise ValueError("invalid sw.js_data response")
    data = json.loads(raw[4:])
    ytcfg = _list_get(_list_get(data, 0, []), 2, [])
    device_info = _list_get(_list_get(ytcfg, 0, []), 0, [])
    api_key = str(_list_get(ytcfg, 1, "") or "").strip() or YOUTUBE_WEB_DEFAULT_API_KEY

    config_info = _list_get(device_info, 61, [])
    app_install_data = None
    if isinstance(config_info, list) and config_info:
        app_install_data = str(config_info[-1] or "").strip() or None

    visitor_data = str(_list_get(device_info, 13, "") or "").strip() or str(visitor_cookie or "").strip()
    client_version = str(_list_get(device_info, 16, "") or "").strip() or YOUTUBE_WEB_DEFAULT_CLIENT_VERSION
    os_name = str(_list_get(device_info, 17, "") or "").strip() or "Windows"
    os_version = str(_list_get(device_info, 18, "") or "").strip() or "10.0"
    browser_name = str(_list_get(device_info, 86, "") or "").strip() or "Chrome"
    browser_version = str(_list_get(device_info, 87, "") or "").strip() or "125.0.0.0"
    device_make = str(_list_get(device_info, 11, "") or "").strip()
    device_model = str(_list_get(device_info, 12, "") or "").strip()
    rollout_token = str(_list_get(device_info, 107, "") or "").strip() or None
    device_experiment_id = str(_list_get(device_info, 103, "") or "").strip() or None
    time_zone = str(_list_get(device_info, 79, "") or "").strip() or str(timezone_name or "UTC").strip() or "UTC"

    return {
        "api_key": api_key,
        "api_version": YOUTUBE_WEB_API_VERSION,
        "context": _build_context(
            visitor_data=visitor_data,
            client_version=client_version,
            user_agent=user_agent,
            os_name=os_name,
            os_version=os_version,
            browser_name=browser_name,
            browser_version=browser_version,
            device_make=device_make,
            device_model=device_model,
            timezone_name=time_zone,
            rollout_token=rollout_token,
            device_experiment_id=device_experiment_id,
            app_install_data=app_install_data,
        ),
    }


def _fallback_session_data(*, user_agent: str, timezone_name: str, visitor_cookie: str) -> dict[str, Any]:
    visitor_data = str(visitor_cookie or "").strip()
    if not visitor_data:
        visitor_data = _random_visitor_id()
    return {
        "api_key": YOUTUBE_WEB_DEFAULT_API_KEY,
        "api_version": YOUTUBE_WEB_API_VERSION,
        "context": _build_context(
            visitor_data=visitor_data,
            client_version=YOUTUBE_WEB_DEFAULT_CLIENT_VERSION,
            user_agent=user_agent,
            os_name="Windows",
            os_version="10.0",
            browser_name="Chrome",
            browser_version="125.0.0.0",
            device_make="",
            device_model="",
            timezone_name=str(timezone_name or "UTC").strip() or "UTC",
        ),
    }


def _make_client(*, user_agent: str, timeout_s: float, proxy: Optional[str]) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": user_agent,
        },
        "timeout": timeout_s,
        "follow_redirects": True,
    }
    proxy = str(proxy or "").strip()
    if proxy:
        kwargs["proxy"] = proxy
    try:
        return httpx.Client(**kwargs)
    except TypeError:
        kwargs.pop("proxy", None)
        return httpx.Client(**kwargs)


def _load_session_data(
    client: httpx.Client,
    *,
    user_agent: str,
    cookie_header: str,
    timezone_name: str,
) -> dict[str, Any]:
    visitor_cookie = str(_get_cookie(cookie_header, "VISITOR_INFO1_LIVE") or "").strip() or _random_visitor_id()
    tz_pref = str(timezone_name or "UTC").replace("/", ".")
    resp = client.get(
        YOUTUBE_SW_DATA_URL,
        headers={
            "Referer": f"{YOUTUBE_ORIGIN}/sw.js",
            "Cookie": f"PREF=tz={tz_pref};VISITOR_INFO1_LIVE={visitor_cookie};",
        },
    )
    resp.raise_for_status()
    return parse_sw_session_data(
        resp.text,
        user_agent=user_agent,
        timezone_name=timezone_name,
        visitor_cookie=visitor_cookie,
    )


def _browse_headers(*, user_agent: str, cookie_header: str, context: dict[str, Any]) -> dict[str, str]:
    client = dict(context.get("client") or {})
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": YOUTUBE_ORIGIN,
        "Referer": YOUTUBE_HOME_URL,
        "User-Agent": user_agent,
        "X-Goog-Visitor-Id": str(client.get("visitorData") or ""),
        "X-Origin": YOUTUBE_ORIGIN,
        "X-Youtube-Client-Name": YOUTUBE_WEB_CLIENT_NAME_ID,
        "X-Youtube-Client-Version": str(client.get("clientVersion") or YOUTUBE_WEB_DEFAULT_CLIENT_VERSION),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
        sid = (
            _get_cookie(cookie_header, "SAPISID")
            or _get_cookie(cookie_header, "__Secure-3PAPISID")
            or _get_cookie(cookie_header, "APISID")
        )
        if sid:
            headers["Authorization"] = _generate_sid_auth(sid)
            headers["X-Goog-Authuser"] = "0"
    return headers


def _browse(
    client: httpx.Client,
    *,
    api_key: str,
    user_agent: str,
    cookie_header: str,
    context: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    resp = client.post(
        YOUTUBE_BROWSE_URL,
        params={"key": api_key, "prettyPrint": "false"},
        headers=_browse_headers(user_agent=user_agent, cookie_header=cookie_header, context=context),
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("youtube browse returned invalid payload")
    return data


def fetch_youtube_home_feed(
    cookies_txt: str,
    user_agent: str,
    *,
    proxy: Optional[str] = None,
    limit: int = 20,
    long_videos_only: bool = False,
    min_duration_seconds: int = YOUTUBE_LONG_VIDEO_MIN_DURATION_SECONDS,
    timezone_name: str = "UTC",
    timeout_s: float = 20.0,
    max_pages: int = 4,
) -> HomeFeedFetchResult:
    target_limit = max(1, _safe_int(limit, 20))
    candidate_limit = target_limit
    if long_videos_only:
        candidate_limit = min(400, max(target_limit * 8, 80))
    cookie_header = _cookie_header_from_netscape(cookies_txt)
    if not cookie_header:
        raise RuntimeError("youtube cookies are empty or do not match youtube.com")

    with _make_client(user_agent=user_agent, timeout_s=timeout_s, proxy=proxy) as client:
        try:
            session = _load_session_data(
                client,
                user_agent=user_agent,
                cookie_header=cookie_header,
                timezone_name=timezone_name,
            )
        except Exception:
            session = _fallback_session_data(
                user_agent=user_agent,
                timezone_name=timezone_name,
                visitor_cookie=str(_get_cookie(cookie_header, "VISITOR_INFO1_LIVE") or ""),
            )

        api_key = str(session.get("api_key") or "").strip() or YOUTUBE_WEB_DEFAULT_API_KEY
        context = dict(session.get("context") or {})
        videos: list[HomeFeedVideo] = []
        seen_video_ids: set[str] = set()
        seen_tokens: set[str] = set()

        data = _browse(
            client,
            api_key=api_key,
            user_agent=user_agent,
            cookie_header=cookie_header,
            context=context,
            payload={"context": context, "browseId": YOUTUBE_HOME_BROWSE_ID},
        )
        for item in extract_home_feed_videos(data, limit=candidate_limit):
            if item.video_id not in seen_video_ids:
                seen_video_ids.add(item.video_id)
                videos.append(item)

        tokens = extract_home_feed_continuations(data)
        for token in tokens:
            seen_tokens.add(token)

        pages = 1
        while tokens and pages < max(1, int(max_pages or 1)):
            if not long_videos_only and len(videos) >= candidate_limit:
                break
            if long_videos_only:
                preview_selected, _preview_stats = _filter_long_videos(
                    videos,
                    min_duration_seconds=min_duration_seconds,
                    requested_limit=target_limit,
                    candidate_limit=candidate_limit,
                )
                if len(videos) >= candidate_limit and len(preview_selected) >= target_limit:
                    break
            token = tokens.pop(0)
            data = _browse(
                client,
                api_key=api_key,
                user_agent=user_agent,
                cookie_header=cookie_header,
                context=context,
                payload={"context": context, "continuation": token},
            )
            for item in extract_home_feed_videos(data, limit=candidate_limit):
                if item.video_id not in seen_video_ids:
                    seen_video_ids.add(item.video_id)
                    videos.append(item)
                    if not long_videos_only and len(videos) >= candidate_limit:
                        break
            for next_token in extract_home_feed_continuations(data):
                if next_token not in seen_tokens:
                    seen_tokens.add(next_token)
                    tokens.append(next_token)
            pages += 1

    if long_videos_only:
        selected, stats = _filter_long_videos(
            videos,
            min_duration_seconds=min_duration_seconds,
            requested_limit=target_limit,
            candidate_limit=candidate_limit,
        )
        return HomeFeedFetchResult(videos=selected, stats=stats)

    returned = videos[:target_limit]
    known_duration_count = sum(1 for item in videos if item.duration_seconds is not None)
    unknown_duration_count = max(0, len(videos) - known_duration_count)
    explicit_shorts_count = sum(1 for item in videos if item.is_short)
    stats = HomeFeedFetchStats(
        requested_limit=target_limit,
        candidate_limit=candidate_limit,
        long_videos_only=False,
        min_duration_seconds=max(0, int(min_duration_seconds or 0)),
        candidate_count=len(videos),
        explicit_shorts_count=explicit_shorts_count,
        known_duration_count=known_duration_count,
        unknown_duration_count=unknown_duration_count,
        eligible_count=len(videos),
        returned_count=len(returned),
        log_lines=(
            f"raw_candidates={len(videos)} candidate_limit={candidate_limit} requested={target_limit}",
            f"explicit_shorts={explicit_shorts_count} known_duration={known_duration_count} unknown_duration={unknown_duration_count}",
            f"eligible={len(videos)} returned={len(returned)}",
        ),
    )
    return HomeFeedFetchResult(videos=returned, stats=stats)
