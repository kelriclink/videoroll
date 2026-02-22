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
        "default_enable_summary": bool(
            stored.get("default_enable_summary") if "default_enable_summary" in stored else defaults.translate_enable_summary
        ),
        "openai_api_key": api_key,
        "openai_api_key_set": bool(api_key),
        "openai_base_url": normalize_openai_base_url(str(openai.get("base_url") or defaults.openai_base_url)),
        "openai_model": str(openai.get("model") or defaults.openai_model),
        "openai_temperature": float(openai.get("temperature") or defaults.openai_temperature),
        "openai_timeout_seconds": float(openai.get("timeout_seconds") or defaults.openai_timeout_seconds),
    }


def update_translate_settings(db: Session, defaults: SubtitleServiceSettings, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    for key in ["default_provider", "default_target_lang", "default_style", "default_batch_size", "default_enable_summary"]:
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
