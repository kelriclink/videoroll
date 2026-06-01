from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


AUTO_PROFILE_KEY = "subtitle.auto_profile"

_ALLOWED_FORMATS = {"srt", "ass"}
_ALLOWED_ASR_ENGINES = {"auto", "mock", "faster-whisper", "openvino"}
_ALLOWED_TRANSLATE_PROVIDERS = {"mock", "noop", "openai"}
_ALLOWED_YOUTUBE_SUBTITLE_MODES = {"off", "target", "auto_source"}
_ALLOWED_ASS_STYLES = {"clean_white"}
_ALLOWED_VIDEO_CODECS = {"av1", "h264"}
_ALLOWED_H264_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
}
_AV1_PRESET_MIN = 0
_AV1_PRESET_MAX = 13
_ALLOWED_PUBLISH_TYPEID_MODES = {"ai_summary", "bilibili_predict", "meta"}
_VIDEO_CRF_MIN = 0
_VIDEO_CRF_MAX = 63
_FONT_SCALE_PERCENT_MIN = 25
_FONT_SCALE_PERCENT_MAX = 300


def _default_profile() -> dict[str, Any]:
    return {
        "formats": ["srt", "ass"],
        "burn_in": True,
        "soft_sub": False,
        "ass_style": "clean_white",
        "video_codec": "av1",
        "use_intel_gpu": False,
        # Optional: if None/empty, codec-specific defaults are used by the worker.
        "video_preset": None,
        # Optional: if None, codec-specific defaults are used by the worker.
        "video_crf": None,
        # Percent of the current adaptive font size (100 = keep existing behavior).
        "primary_font_scale_percent": 100,
        "secondary_font_scale_percent": 100,
        "asr_engine": "auto",
        "asr_language": "auto",
        "asr_model": None,
        "prefer_youtube_subtitles": True,
        "youtube_subtitle_mode": "target",
        "translate_enabled": True,
        "translate_provider": "openai",
        "target_lang": "zh",
        "translate_style": "口语自然",
        "translate_enable_summary": True,
        "bilingual": False,
        "auto_publish": True,
        "publish_typeid_mode": "ai_summary",
        "publish_title_prefix": "【熟肉】",
        "publish_translate_title": True,
        "publish_use_youtube_cover": True,
        # If false, publish as "自制" (copyright=1) while keeping the original link in description.
        "publish_enable_reprint": True,
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


def _normalize_youtube_subtitle_mode(val: Any, *, fallback: str) -> str:
    s = str(val or "").strip().lower()
    return s if s in _ALLOWED_YOUTUBE_SUBTITLE_MODES else fallback


def _normalize_video_crf(val: Any) -> int | None:
    if val is None:
        return None
    try:
        n = int(val)
    except Exception:
        return None
    if n < _VIDEO_CRF_MIN:
        return _VIDEO_CRF_MIN
    if n > _VIDEO_CRF_MAX:
        return _VIDEO_CRF_MAX
    return n


def _normalize_video_preset(val: Any, *, codec: str) -> str | None:
    if val is None:
        return None
    s = str(val or "").strip()
    if not s:
        return None

    if codec in {"h264", "avc"}:
        preset = s.lower()
        return preset if preset in _ALLOWED_H264_PRESETS else None

    # AV1 (SVT-AV1) uses numeric preset 0..13.
    try:
        n = int(s)
    except Exception:
        return None
    if n < _AV1_PRESET_MIN:
        n = _AV1_PRESET_MIN
    if n > _AV1_PRESET_MAX:
        n = _AV1_PRESET_MAX
    return str(n)


def _normalize_font_scale_percent(val: Any, *, fallback: int) -> int:
    try:
        n = int(val)
    except Exception:
        return fallback
    if n < _FONT_SCALE_PERCENT_MIN:
        return _FONT_SCALE_PERCENT_MIN
    if n > _FONT_SCALE_PERCENT_MAX:
        return _FONT_SCALE_PERCENT_MAX
    return n


def get_auto_profile(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, AUTO_PROFILE_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    baseline = _default_profile()
    merged: dict[str, Any] = {**baseline, **stored}

    merged["formats"] = _normalize_formats(merged.get("formats"), fallback=baseline["formats"])
    merged["burn_in"] = bool(merged.get("burn_in"))
    merged["soft_sub"] = bool(merged.get("soft_sub"))
    merged["use_intel_gpu"] = bool(merged.get("use_intel_gpu"))

    ass_style = str(merged.get("ass_style") or baseline["ass_style"]).strip() or baseline["ass_style"]
    merged["ass_style"] = ass_style if ass_style in _ALLOWED_ASS_STYLES else baseline["ass_style"]

    merged["video_codec"] = _normalize_video_codec(merged.get("video_codec"), fallback=baseline["video_codec"])
    merged["video_preset"] = _normalize_video_preset(merged.get("video_preset"), codec=merged["video_codec"])
    merged["video_crf"] = _normalize_video_crf(merged.get("video_crf"))
    merged["primary_font_scale_percent"] = _normalize_font_scale_percent(
        merged.get("primary_font_scale_percent"),
        fallback=baseline["primary_font_scale_percent"],
    )
    merged["secondary_font_scale_percent"] = _normalize_font_scale_percent(
        merged.get("secondary_font_scale_percent"),
        fallback=baseline["secondary_font_scale_percent"],
    )

    asr_engine = str(merged.get("asr_engine") or baseline["asr_engine"]).strip() or baseline["asr_engine"]
    merged["asr_engine"] = asr_engine if asr_engine in _ALLOWED_ASR_ENGINES else baseline["asr_engine"]
    merged["asr_language"] = str(merged.get("asr_language") or baseline["asr_language"]).strip() or baseline["asr_language"]

    asr_model = merged.get("asr_model")
    asr_model = str(asr_model).strip() if asr_model is not None else ""
    merged["asr_model"] = asr_model or None

    prefer_youtube_subtitles = bool(merged.get("prefer_youtube_subtitles", baseline["prefer_youtube_subtitles"]))
    youtube_subtitle_mode_fallback = baseline["youtube_subtitle_mode"] if prefer_youtube_subtitles else "off"
    merged["youtube_subtitle_mode"] = _normalize_youtube_subtitle_mode(
        merged.get("youtube_subtitle_mode"),
        fallback=youtube_subtitle_mode_fallback,
    )
    merged["prefer_youtube_subtitles"] = merged["youtube_subtitle_mode"] != "off"
    merged["translate_enabled"] = bool(merged.get("translate_enabled"))
    provider = str(merged.get("translate_provider") or baseline["translate_provider"]).strip() or baseline["translate_provider"]
    merged["translate_provider"] = provider if provider in _ALLOWED_TRANSLATE_PROVIDERS else baseline["translate_provider"]
    merged["target_lang"] = str(merged.get("target_lang") or baseline["target_lang"]).strip() or baseline["target_lang"]
    merged["translate_style"] = str(merged.get("translate_style") or baseline["translate_style"]).strip() or baseline["translate_style"]
    merged["translate_enable_summary"] = bool(merged.get("translate_enable_summary"))
    merged["bilingual"] = bool(merged.get("bilingual"))

    merged["auto_publish"] = bool(merged.get("auto_publish"))
    publish_typeid_mode = str(merged.get("publish_typeid_mode") or baseline["publish_typeid_mode"]).strip() or baseline["publish_typeid_mode"]
    merged["publish_typeid_mode"] = publish_typeid_mode if publish_typeid_mode in _ALLOWED_PUBLISH_TYPEID_MODES else baseline["publish_typeid_mode"]
    merged["publish_title_prefix"] = str(merged.get("publish_title_prefix") or baseline["publish_title_prefix"]).strip() or baseline[
        "publish_title_prefix"
    ]
    merged["publish_translate_title"] = bool(merged.get("publish_translate_title"))
    merged["publish_use_youtube_cover"] = bool(merged.get("publish_use_youtube_cover"))
    merged["publish_enable_reprint"] = bool(merged.get("publish_enable_reprint"))
    return merged


def update_auto_profile(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "formats" in update and update["formats"] is not None:
        stored["formats"] = update["formats"]
    for k in [
        "burn_in",
        "soft_sub",
        "use_intel_gpu",
        "prefer_youtube_subtitles",
        "translate_enabled",
        "translate_enable_summary",
        "bilingual",
        "auto_publish",
        "publish_translate_title",
        "publish_use_youtube_cover",
        "publish_enable_reprint",
    ]:
        if k in update and update[k] is not None:
            stored[k] = bool(update[k])

    for k in [
        "ass_style",
        "video_codec",
        "asr_engine",
        "asr_language",
        "youtube_subtitle_mode",
        "translate_provider",
        "target_lang",
        "translate_style",
        "publish_title_prefix",
    ]:
        if k in update and update[k] is not None:
            stored[k] = update[k]
    if "youtube_subtitle_mode" in update and update["youtube_subtitle_mode"] is not None:
        stored["prefer_youtube_subtitles"] = str(update["youtube_subtitle_mode"]).strip().lower() != "off"
    if "video_preset" in update:
        val = update["video_preset"]
        if val is None:
            stored["video_preset"] = None
        else:
            s = str(val or "").strip()
            stored["video_preset"] = s or None
    if "video_crf" in update:
        stored["video_crf"] = update["video_crf"]
    for k in ["primary_font_scale_percent", "secondary_font_scale_percent"]:
        if k in update and update[k] is not None:
            stored[k] = update[k]
    if "publish_typeid_mode" in update and update["publish_typeid_mode"] is not None:
        stored["publish_typeid_mode"] = update["publish_typeid_mode"]

    if "asr_model" in update and update["asr_model"] is not None:
        val = str(update["asr_model"]).strip()
        stored["asr_model"] = val or None

    row.value_json = stored
    db.add(row)
    db.commit()
    return get_auto_profile(db)
