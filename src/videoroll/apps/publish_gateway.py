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


def _douyin_original_author(meta: Mapping[str, Any], original_author: object) -> str:
    author = str(original_author or "").strip()
    if author:
        return author
    description = str(meta.get("desc") or meta.get("description") or "")
    for raw_line in description.splitlines():
        line = raw_line.strip()
        for prefix in ("原作者：", "原作者:", "博主：", "博主:", "作者：", "作者:", "UP：", "UP:"):
            if line.startswith(prefix):
                author = line[len(prefix) :].strip()
                if author:
                    return author
    return ""


def _strip_author_suffix(title: str, author: str) -> str:
    if not author:
        return title
    folded_title = title.casefold()
    for separator in (" - ", " — ", " – ", "-", "—", "–"):
        suffix = f"{separator}{author}"
        if folded_title.endswith(suffix.casefold()):
            trimmed = title[: -len(suffix)].strip()
            if trimmed:
                return trimmed
    return title


def normalize_social_publish_meta(
    meta: Mapping[str, Any],
    platform: object,
    *,
    original_author: object = "",
) -> dict[str, Any]:
    value = normalize_publish_platform(platform)
    if value not in SUPPORTED_SOCIAL_PLATFORMS:
        raise ValueError(f"social metadata is not supported for platform: {value}")
    title = str(meta.get("title") or "").strip()
    if not title:
        raise ValueError("meta.title is required")
    tags = _normalize_tags(meta.get("tags"))
    if value == "douyin":
        author = _douyin_original_author(meta, original_author)
        return {
            "title": _strip_author_suffix(title, author),
            "desc": f"原作者：{author or '未提供'}"[:1000],
            "tags": [tag for tag in tags if tag.casefold() != "videoroll"][:4],
        }
    if value == "xiaohongshu" and len(tags) > 10:
        raise ValueError("xiaohongshu accepts at most 10 tags")
    return {
        "title": title,
        "desc": str(meta.get("desc") or meta.get("description") or "").strip(),
        "tags": tags,
    }
