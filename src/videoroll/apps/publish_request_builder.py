from __future__ import annotations

import json
import uuid
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta
from videoroll.apps.publish_gateway import (
    normalize_publish_platform,
    normalize_social_publish_meta,
    publish_meta_key,
)
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_tags
from videoroll.apps.youtube_meta_store import get_task_youtube_meta
from videoroll.db.models import Task
from videoroll.storage.s3 import S3Store


def publish_meta_s3_key(task_id: uuid.UUID) -> str:
    return f"meta/{task_id}/publish_meta.json"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _value(payload: Any, name: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(name, default)
    return getattr(payload, name, default)


def _read_json(s3: S3Store, key: str) -> dict[str, Any] | None:
    try:
        obj = s3.get_object(key)
    except ClientError as exc:
        code = str((_as_dict(exc.response.get("Error")).get("Code") or "")).strip()
        if code in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    body = obj.get("Body")
    if not body:
        return None
    try:
        raw = body.read() or b""
    finally:
        try:
            body.close()
        except Exception:
            pass
    if not raw:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in (item.strip() for item in value.replace("，", ",").split(",")) if part]
    if isinstance(value, (list, tuple, set)):
        return [text for text in (str(item or "").strip() for item in value) if text]
    text = str(value or "").strip()
    return [text] if text else []


def prepare_bilibili_publish_meta(
    *,
    task: Task,
    payload_meta: dict[str, Any] | None,
    db: Session,
    s3: S3Store,
    allow_auto_draft: bool = False,
) -> dict[str, Any]:
    """Validate Bilibili metadata without importing the orchestrator layer."""
    if payload_meta is None:
        meta = _read_json(s3, publish_meta_s3_key(task.id))
        if meta is None:
            if allow_auto_draft:
                raise ValueError("meta is missing and automatic draft generation is unavailable")
            raise ValueError("meta is missing and publish_meta is not found")
    else:
        meta = dict(payload_meta)

    try:
        copyright_value = int(meta.get("copyright") or 1)
    except Exception:
        copyright_value = 1
    if copyright_value == 2 and not str(meta.get("source") or "").strip() and task.source_url:
        meta["source"] = task.source_url

    merged_tags: list[str] = []
    seen: set[str] = set()
    for tag in ["videoroll", *get_task_bilibili_tags(db, str(task.id)), *_normalize_tags(meta.get("tags"))]:
        text = str(tag or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        merged_tags.append(text)
        if len(merged_tags) >= 10:
            break
    if merged_tags:
        meta["tags"] = merged_tags

    try:
        return BilibiliPublishMeta.model_validate(meta).model_dump()
    except Exception as exc:
        raise ValueError(f"invalid publish meta: {exc}") from exc


def build_publish_gateway_request(
    *,
    task: Task,
    task_id: uuid.UUID,
    payload: Any,
    video_key: str,
    db: Session,
    s3: S3Store,
) -> dict[str, Any]:
    """Build a publisher request from a plain dict or API request model."""
    platform = normalize_publish_platform(_value(payload, "platform"))
    account_id = _value(payload, "account_id")
    if platform != "bilibili":
        try:
            uuid.UUID(str(account_id or ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("social publish account_id must be a UUID") from exc

    payload_meta = _value(payload, "meta")
    if platform == "bilibili":
        meta = prepare_bilibili_publish_meta(
            task=task,
            payload_meta=payload_meta,
            db=db,
            s3=s3,
        )
    else:
        meta_source = payload_meta
        if meta_source is None:
            meta_source = _read_json(s3, publish_meta_key(task_id, platform))
        if meta_source is None:
            meta_source = _read_json(s3, publish_meta_s3_key(task_id))
        if meta_source is None:
            raise ValueError("meta is missing and platform publish meta is not found")
        try:
            youtube_meta = get_task_youtube_meta(db, task_id) if platform == "douyin" else None
            original_author = str(getattr(youtube_meta, "uploader", "") or "").strip()
            meta = normalize_social_publish_meta(
                _as_dict(meta_source),
                platform,
                original_author=original_author,
            )
        except ValueError as exc:
            raise ValueError(f"invalid publish meta: {exc}") from exc

    all_options = _value(payload, "platform_options", {})
    platform_options = _as_dict(_as_dict(all_options).get(platform))
    request: dict[str, Any] = {
        "platform": platform,
        "task_id": str(task_id),
        "account_id": account_id,
        "video": {"type": "s3", "key": video_key},
        "cover": {"type": "s3", "key": _value(payload, "cover_key")} if _value(payload, "cover_key") else None,
        "meta": meta,
        "platform_options": platform_options,
    }
    if platform != "bilibili":
        request["force_retry"] = bool(_value(payload, "force_retry", False))
    typeid_mode = str(platform_options.get("typeid_mode") or _value(payload, "typeid_mode") or "").strip()
    if typeid_mode:
        request["typeid_mode"] = typeid_mode
    return request
