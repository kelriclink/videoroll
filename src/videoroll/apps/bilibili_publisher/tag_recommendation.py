"""Resolve Bilibili publish tags using AI results, official recommendations, then fallback.

Only auto-tag results are filtered; manually supplied tags are never deleted.
The authenticated creator-center API is GET-only and is used only if both the
task's generated tags and the explicit metadata tags are empty.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.bilibili_publisher.auth_settings_store import get_bilibili_cookie_header
from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient
from videoroll.apps.publish_gateway import merge_bilibili_tags

logger = logging.getLogger(__name__)
_FALLBACK = "视频分享"
_MISLEADING_EXTERNAL = frozenset({"原创", "首发", "独家", "自制", "独家首发"})
_GENERIC_RECOMMENDATIONS = frozenset({"生活记录", "记录", "热门", "视频分享", "分享"})


def resolve_bilibili_publish_tags(
    db: Session,
    *,
    generated: list[str],
    explicit: list[str],
    title: str,
    description: str,
    typeid: int,
    external_source: bool,
) -> list[str]:
    """AI tags > Bilibili official recommended tags > generic fallback.

    Existing tags win; user-written tags (including "videoroll") survive.
    Never assume a topic/mission recommendation is a plain video tag.
    """
    generated_clean = [
        tag for tag in generated
        if str(tag or "").strip().casefold() != "videoroll"
        and not (external_source and str(tag or "").strip() in _MISLEADING_EXTERNAL)
    ]
    chosen = merge_bilibili_tags(generated_clean, explicit)
    if chosen:
        return chosen

    tags: list[str] = []
    try:
        cookie = get_bilibili_cookie_header(db)
        if cookie:
            with BilibiliWebClient(cookie) as client:
                tags = client.recommended_tags(
                    type_id=int(typeid or 0),
                    title=str(title or ""),
                    description=str(description or ""),
                )
    except Exception as exc:
        # Upstream availability is not grounds for failing the upload.
        # No request headers/cookies or full URLs in logs.
        logger.warning("Bilibili tag recommendation unavailable: %s", type(exc).__name__)

    auto_tags: list[str] = []
    for tag in tags:
        candidate = str(tag or "").strip().lstrip("#＃").strip()
        if (not candidate or candidate.casefold() == "videoroll"
                or candidate in _GENERIC_RECOMMENDATIONS or len(candidate) > 20):
            continue
        if external_source and candidate in _MISLEADING_EXTERNAL:
            continue
        auto_tags.append(candidate)
    chosen = merge_bilibili_tags(auto_tags, [])
    return chosen or [_FALLBACK]
