from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


AUTO_PROFILE_KEY = "subtitle.auto_profile"

_ALLOWED_FORMATS = {"srt", "ass"}
_ALLOWED_ASR_ENGINES = {"auto", "mock", "faster-whisper"}
_ALLOWED_TRANSLATE_PROVIDERS = {"mock", "noop", "openai"}
_ALLOWED_ASS_STYLES = {"clean_white"}
_ALLOWED_VIDEO_CODECS = {"av1", "h264"}


def _default_profile() -> dict[str, Any]:
    return {
        "formats": ["srt", "ass"],
        "burn_in": True,
        "soft_sub": False,
        "ass_style": "clean_white",
        "video_codec": "av1",
        "asr_engine": "auto",
        "asr_language": "auto",
        "asr_model": None,
        "translate_enabled": True,
        "translate_provider": "openai",
        "target_lang": "zh",
        "translate_style": "口语自然",
        "translate_enable_summary": True,
        "bilingual": False,
        "auto_publish": True,
        "publish_title_prefix": "【熟肉】",
        "publish_translate_title": True,
        "publish_use_youtube_cover": True,
    }


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, AUTO_PROFILE_KEY)
    if row:
        return row
    row = AppSetting(key=AUTO_PROFILE_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _normalize_formats(val: Any, *, fallback: list[str]) -> list[str]:
    if not isinstance(val, list):
        return fallback
    out: list[str] = []
    for item in val:
        s = str(item or "").strip().lower()
        if s in _ALLOWED_FORMATS and s not in out:
            out.append(s)
    return out or fallback


def _normalize_video_codec(val: Any, *, fallback: str) -> str:
    s = str(val or "").strip().lower()
    return s if s in _ALLOWED_VIDEO_CODECS else fallback


def get_auto_profile(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, AUTO_PROFILE_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    baseline = _default_profile()
    merged: dict[str, Any] = {**baseline, **stored}

    merged["formats"] = _normalize_formats(merged.get("formats"), fallback=baseline["formats"])
    merged["burn_in"] = bool(merged.get("burn_in"))
    merged["soft_sub"] = bool(merged.get("soft_sub"))

    ass_style = str(merged.get("ass_style") or baseline["ass_style"]).strip() or baseline["ass_style"]
    merged["ass_style"] = ass_style if ass_style in _ALLOWED_ASS_STYLES else baseline["ass_style"]

    merged["video_codec"] = _normalize_video_codec(merged.get("video_codec"), fallback=baseline["video_codec"])

    asr_engine = str(merged.get("asr_engine") or baseline["asr_engine"]).strip() or baseline["asr_engine"]
    merged["asr_engine"] = asr_engine if asr_engine in _ALLOWED_ASR_ENGINES else baseline["asr_engine"]
    merged["asr_language"] = str(merged.get("asr_language") or baseline["asr_language"]).strip() or baseline["asr_language"]

    asr_model = merged.get("asr_model")
    asr_model = str(asr_model).strip() if asr_model is not None else ""
    merged["asr_model"] = asr_model or None

    merged["translate_enabled"] = bool(merged.get("translate_enabled"))
    provider = str(merged.get("translate_provider") or baseline["translate_provider"]).strip() or baseline["translate_provider"]
    merged["translate_provider"] = provider if provider in _ALLOWED_TRANSLATE_PROVIDERS else baseline["translate_provider"]
    merged["target_lang"] = str(merged.get("target_lang") or baseline["target_lang"]).strip() or baseline["target_lang"]
    merged["translate_style"] = str(merged.get("translate_style") or baseline["translate_style"]).strip() or baseline["translate_style"]
    merged["translate_enable_summary"] = bool(merged.get("translate_enable_summary"))
    merged["bilingual"] = bool(merged.get("bilingual"))

    merged["auto_publish"] = bool(merged.get("auto_publish"))
    merged["publish_title_prefix"] = str(merged.get("publish_title_prefix") or baseline["publish_title_prefix"]).strip() or baseline[
        "publish_title_prefix"
    ]
    merged["publish_translate_title"] = bool(merged.get("publish_translate_title"))
    merged["publish_use_youtube_cover"] = bool(merged.get("publish_use_youtube_cover"))
    return merged


def update_auto_profile(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "formats" in update and update["formats"] is not None:
        stored["formats"] = update["formats"]
    for k in ["burn_in", "soft_sub", "translate_enabled", "translate_enable_summary", "bilingual", "auto_publish", "publish_translate_title", "publish_use_youtube_cover"]:
        if k in update and update[k] is not None:
            stored[k] = bool(update[k])

    for k in ["ass_style", "video_codec", "asr_engine", "asr_language", "translate_provider", "target_lang", "translate_style", "publish_title_prefix"]:
        if k in update and update[k] is not None:
            stored[k] = update[k]

    if "asr_model" in update and update["asr_model"] is not None:
        val = str(update["asr_model"]).strip()
        stored["asr_model"] = val or None

    row.value_json = stored
    db.add(row)
    db.commit()
    return get_auto_profile(db)
