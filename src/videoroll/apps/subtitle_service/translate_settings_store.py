from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.config import SubtitleServiceSettings
from videoroll.db.models import AppSetting
from videoroll.utils.fernet import decrypt_str, encrypt_str
from videoroll.utils.openai_compat import normalize_openai_base_url


TRANSLATE_SETTINGS_KEY = "subtitle.translate"


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, TRANSLATE_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=TRANSLATE_SETTINGS_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def get_translate_settings(db: Session, defaults: SubtitleServiceSettings) -> dict[str, Any]:
    row = db.get(AppSetting, TRANSLATE_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    openai = _as_dict(stored.get("openai"))
    api_key = ""
    api_key_enc = openai.get("api_key_enc")
    if isinstance(api_key_enc, str) and api_key_enc.strip():
        try:
            api_key = decrypt_str(api_key_enc)
        except Exception:
            api_key = ""

    return {
        "default_provider": str(stored.get("default_provider") or defaults.translate_default_provider),
        "default_target_lang": str(stored.get("default_target_lang") or defaults.translate_default_target_lang),
        "default_style": str(stored.get("default_style") or defaults.translate_default_style),
        "default_batch_size": int(stored.get("default_batch_size") or defaults.translate_batch_size),
        "default_max_retries": int(stored.get("default_max_retries") or defaults.translate_max_retries),
        "default_enable_summary": bool(
            stored.get("default_enable_summary") if "default_enable_summary" in stored else defaults.translate_enable_summary
        ),
        "openai_api_key": api_key,
        "openai_api_key_set": bool(api_key),
        "openai_base_url": normalize_openai_base_url(str(openai.get("base_url") or defaults.openai_base_url)),
        "openai_model": str(openai.get("model") or defaults.openai_model),
        "openai_temperature": float(openai.get("temperature") or defaults.openai_temperature),
        "openai_timeout_seconds": float(openai.get("timeout_seconds") or defaults.openai_timeout_seconds),
        "rag_enabled": bool(stored.get("rag_enabled") if "rag_enabled" in stored else defaults.rag_enabled),
        "rag_top_k": int(stored.get("rag_top_k") or defaults.rag_top_k),
        "rag_min_score": float(stored.get("rag_min_score") or defaults.rag_min_score),
        "rag_embedding_provider": str(stored.get("rag_embedding_provider") or defaults.rag_embedding_provider),
        "rag_embedding_model": str(stored.get("rag_embedding_model") or defaults.rag_embedding_model),
        "rag_embedding_dimensions": int(stored.get("rag_embedding_dimensions") or defaults.rag_embedding_dimensions),
        "rag_embedding_model_dir": str(stored.get("rag_embedding_model_dir") or defaults.rag_embedding_model_dir),
        "rag_embedding_device": str(stored.get("rag_embedding_device") or defaults.rag_embedding_device),
        "rag_auto_discover_terms": bool(
            stored.get("rag_auto_discover_terms") if "rag_auto_discover_terms" in stored else defaults.rag_auto_discover_terms
        ),
        "rag_auto_learn_terms": bool(stored.get("rag_auto_learn_terms") if "rag_auto_learn_terms" in stored else defaults.rag_auto_learn_terms),
        "rag_search_enabled": bool(stored.get("rag_search_enabled") if "rag_search_enabled" in stored else defaults.rag_search_enabled),
        "rag_search_url": str(stored.get("rag_search_url") or defaults.rag_search_url),
        "rag_domain": str(stored.get("rag_domain") or defaults.rag_domain),
    }


def update_translate_settings(db: Session, defaults: SubtitleServiceSettings, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    for key in [
        "default_provider",
        "default_target_lang",
        "default_style",
        "default_batch_size",
        "default_max_retries",
        "default_enable_summary",
        "rag_enabled",
        "rag_top_k",
        "rag_min_score",
        "rag_embedding_provider",
        "rag_embedding_model",
        "rag_embedding_dimensions",
        "rag_embedding_model_dir",
        "rag_embedding_device",
        "rag_auto_discover_terms",
        "rag_auto_learn_terms",
        "rag_search_enabled",
        "rag_search_url",
        "rag_domain",
    ]:
        if key not in update:
            continue
        val = update.get(key)
        if val is None:
            continue
        stored[key] = val

    openai = dict(_as_dict(stored.get("openai")))

    if "openai_api_key" in update:
        key = update.get("openai_api_key")
        if key is None:
            pass
        else:
            key = str(key).strip()
            if not key:
                openai.pop("api_key_enc", None)
            else:
                openai["api_key_enc"] = encrypt_str(key)

    for key, stored_key in [
        ("openai_base_url", "base_url"),
        ("openai_model", "model"),
        ("openai_temperature", "temperature"),
        ("openai_timeout_seconds", "timeout_seconds"),
    ]:
        if key not in update:
            continue
        val = update.get(key)
        if val is None:
            continue
        if key == "openai_base_url":
            openai[stored_key] = normalize_openai_base_url(str(val))
        else:
            openai[stored_key] = val

    stored["openai"] = openai

    # Normalize a few types.
    try:
        if "default_batch_size" in stored and stored["default_batch_size"] is not None:
            stored["default_batch_size"] = max(1, int(stored["default_batch_size"]))
    except Exception:
        stored["default_batch_size"] = defaults.translate_batch_size
    try:
        if "default_max_retries" in stored and stored["default_max_retries"] is not None:
            stored["default_max_retries"] = max(0, min(10, int(stored["default_max_retries"])))
    except Exception:
        stored["default_max_retries"] = defaults.translate_max_retries
    try:
        stored["rag_top_k"] = max(0, min(30, int(stored.get("rag_top_k") or defaults.rag_top_k)))
    except Exception:
        stored["rag_top_k"] = defaults.rag_top_k
    try:
        stored["rag_min_score"] = max(0.0, min(1.0, float(stored.get("rag_min_score") or defaults.rag_min_score)))
    except Exception:
        stored["rag_min_score"] = defaults.rag_min_score
    try:
        stored["rag_embedding_dimensions"] = max(1, min(4096, int(stored.get("rag_embedding_dimensions") or defaults.rag_embedding_dimensions)))
    except Exception:
        stored["rag_embedding_dimensions"] = defaults.rag_embedding_dimensions
    for bool_key in ["rag_enabled", "rag_auto_discover_terms", "rag_auto_learn_terms", "rag_search_enabled"]:
        if bool_key in stored:
            stored[bool_key] = bool(stored[bool_key])
    provider = str(stored.get("rag_embedding_provider") or defaults.rag_embedding_provider).strip().lower()
    stored["rag_embedding_provider"] = provider if provider in {"openai", "local"} else defaults.rag_embedding_provider
    for str_key in ["rag_embedding_model", "rag_embedding_model_dir", "rag_embedding_device", "rag_search_url", "rag_domain"]:
        if str_key in stored and stored[str_key] is not None:
            stored[str_key] = str(stored[str_key]).strip()
    try:
        if "openai_temperature" in update and update["openai_temperature"] is not None:
            openai["temperature"] = float(openai.get("temperature"))
    except Exception:
        openai["temperature"] = defaults.openai_temperature
    try:
        if "openai_timeout_seconds" in update and update["openai_timeout_seconds"] is not None:
            openai["timeout_seconds"] = float(openai.get("timeout_seconds"))
    except Exception:
        openai["timeout_seconds"] = defaults.openai_timeout_seconds

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_translate_settings(db, defaults)
