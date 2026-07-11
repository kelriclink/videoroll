from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any


SUPPORTED_SOCIAL_PLATFORMS = frozenset({"douyin", "xiaohongshu", "kuaishou"})
SUPPORTED_PUBLISH_PLATFORMS = frozenset({"bilibili", *SUPPORTED_SOCIAL_PLATFORMS})


def normalize_publish_platform(platform: object) -> str:
    value = str(platform or "bilibili").strip().lower() or "bilibili"
    if value not in SUPPORTED_PUBLISH_PLATFORMS:
        raise ValueError(f"unsupported publish platform: {value}")
    return value


def publish_backend_url(settings: object, platform: object) -> str:
    value = normalize_publish_platform(platform)
    if value == "bilibili":
        base = str(getattr(settings, "bilibili_publisher_url")).rstrip("/")
        return f"{base}/bilibili/publish"
    base = str(getattr(settings, "social_publisher_url")).rstrip("/")
    return f"{base}/sau/{value}/publish"


def publish_meta_key(task_id: uuid.UUID, platform: object) -> str:
    return f"meta/{task_id}/publish/{normalize_publish_platform(platform)}.json"


def _normalize_tags(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "").replace("，", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = str(item or "").strip().lstrip("#")
        key = tag.lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def normalize_social_publish_meta(meta: Mapping[str, Any], platform: object) -> dict[str, Any]:
    value = normalize_publish_platform(platform)
    if value not in SUPPORTED_SOCIAL_PLATFORMS:
        raise ValueError(f"social metadata is not supported for platform: {value}")
    title = str(meta.get("title") or "").strip()
    if not title:
        raise ValueError("meta.title is required")
    tags = _normalize_tags(meta.get("tags"))
    if value == "xiaohongshu" and len(tags) > 10:
        raise ValueError("xiaohongshu accepts at most 10 tags")
    return {
        "title": title,
        "desc": str(meta.get("desc") or meta.get("description") or "").strip(),
        "tags": tags,
    }
