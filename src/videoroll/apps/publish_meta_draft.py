from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from videoroll.ai.client import openai_chat_config_from_settings
from videoroll.ai.service import AIService, translate_text_openai
from videoroll.apps.bilibili_publisher.publish_settings_store import get_bilibili_publish_settings
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta
from videoroll.apps.orchestrator_api.youtube_downloader import summarize_info
from videoroll.apps.publish_meta_rules import (
    apply_publish_source_overrides as apply_publish_source_overrides_rules,
    clamp_text,
    has_cjk,
)
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.subtitle_service.task_title_store import get_task_titles
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.config import get_subtitle_settings
from videoroll.db.models import Asset, AssetKind, Task
from videoroll.storage.s3 import S3Store


PublishMetaDraftMode = Literal["auto", "default", "source"]


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _normalize_publish_meta_draft(meta: dict[str, Any]) -> dict[str, Any]:
    try:
        model = BilibiliPublishMeta.model_validate(dict(_as_dict(meta)))
        return model.model_dump()
    except Exception:
        return dict(_as_dict(meta))


def translate_publish_title(
    title: str,
    *,
    profile: dict[str, Any],
    translate_settings: dict[str, Any],
    ai_service: AIService | None = None,
) -> str:
    title_in = str(title or "").strip()
    if not title_in:
        return title
    if has_cjk(title_in):
        return title_in
    if not bool(profile.get("publish_translate_title")):
        return title_in

    provider = str(profile.get("translate_provider") or translate_settings.get("default_provider") or "").strip() or "openai"
    if provider != "openai" or not translate_settings.get("openai_api_key"):
        return title_in

    try:
        if ai_service is not None:
            return ai_service.translate_text(
                title_in,
                target_lang=str(profile.get("target_lang") or translate_settings.get("default_target_lang") or "zh"),
                style=str(profile.get("translate_style") or translate_settings.get("default_style") or "口语自然"),
            )
        return translate_text_openai(
            title_in,
            target_lang=str(profile.get("target_lang") or translate_settings.get("default_target_lang") or "zh"),
            style=str(profile.get("translate_style") or translate_settings.get("default_style") or "口语自然"),
            config=openai_chat_config_from_settings(translate_settings),
        )
    except Exception:
        return title_in


def apply_publish_source_overrides(
    meta: dict[str, Any],
    *,
    source_title: str,
    source_description: str,
    source_url: str,
    source_uploader: str = "",
    profile: dict[str, Any],
    translate_settings: dict[str, Any],
    translated_title: str | None = None,
    ai_service: AIService | None = None,
) -> dict[str, Any]:
    return _normalize_publish_meta_draft(
        apply_publish_source_overrides_rules(
            dict(_as_dict(meta)),
            source_title=source_title,
            translated_title=translated_title,
            source_description=source_description,
            source_url=source_url,
            source_uploader=source_uploader,
            title_prefix=str(profile.get("publish_title_prefix") or "").strip(),
            enable_reprint=bool(profile.get("publish_enable_reprint", True)),
            title_transform=lambda title: translate_publish_title(
                title,
                profile=profile,
                translate_settings=translate_settings,
                ai_service=ai_service,
            ),
        )
    )


def _read_s3_bytes(store: S3Store, key: str) -> bytes:
    obj = store.get_object(key)
    body = obj.get("Body")
    if not body:
        return b""
    try:
        return body.read() or b""
    finally:
        try:
            body.close()
        except Exception:
            pass


def _read_latest_youtube_meta(task_id: uuid.UUID, db: Session, s3: S3Store, *, fallback_url: str) -> dict[str, str] | None:
    asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not asset:
        return None

    try:
        raw = _read_s3_bytes(s3, asset.storage_key)
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        meta = summarize_info(_as_dict(parsed), fallback_url=fallback_url)
    except (ClientError, ValueError, TypeError):
        return None
    except Exception:
        return None

    return {
        "title": str(getattr(meta, "title", "") or "").strip(),
        "description": str(getattr(meta, "description", "") or "").strip(),
        "webpage_url": str(getattr(meta, "webpage_url", "") or fallback_url or "").strip(),
        "uploader": str(getattr(meta, "uploader", "") or "").strip(),
    }


def default_publish_meta(db: Session) -> dict[str, Any]:
    meta_model = get_bilibili_publish_settings(db)["default_meta"]
    return meta_model.model_dump() if hasattr(meta_model, "model_dump") else dict(meta_model)


def build_task_publish_meta_draft(
    task: Task,
    *,
    db: Session,
    s3: S3Store,
    mode: PublishMetaDraftMode = "auto",
    base_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta_out = {**default_publish_meta(db), **_as_dict(base_meta)}
    if mode == "default":
        return _normalize_publish_meta_draft(meta_out)

    titles = get_task_titles(db, str(task.id))
    display_title = str(titles.get("translated_title") or titles.get("source_title") or "").strip()
    if display_title and not str(meta_out.get("title") or "").strip():
        meta_out["title"] = clamp_text(display_title, 80)

    if task.source_type.value != "youtube":
        return _normalize_publish_meta_draft(meta_out)

    should_apply_source = mode == "source" or (mode == "auto" and not _as_dict(base_meta))
    if not should_apply_source:
        return _normalize_publish_meta_draft(meta_out)

    fallback_url = str(task.source_url or "").strip()
    yt_meta = _read_latest_youtube_meta(task.id, db, s3, fallback_url=fallback_url) or {}
    source_title = str(titles.get("source_title") or yt_meta.get("title") or "").strip()
    translated_title = str(titles.get("translated_title") or "").strip() or None
    source_description = str(yt_meta.get("description") or "").strip()
    source_url = str(yt_meta.get("webpage_url") or fallback_url or "").strip()
    source_uploader = str(yt_meta.get("uploader") or "").strip()

    profile = get_auto_profile(db)
    translate_settings = get_translate_settings(db, get_subtitle_settings())
    ai_service = AIService(lambda: get_translate_settings(db, get_subtitle_settings()))
    return apply_publish_source_overrides(
        meta_out,
        source_title=source_title,
        translated_title=translated_title,
        source_description=source_description,
        source_url=source_url,
        source_uploader=source_uploader,
        profile=profile,
        translate_settings=translate_settings,
        ai_service=ai_service,
    )
