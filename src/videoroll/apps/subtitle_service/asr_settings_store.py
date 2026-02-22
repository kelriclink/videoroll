from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.config import SubtitleServiceSettings
from videoroll.db.models import AppSetting


ASR_SETTINGS_KEY = "subtitle.asr"

_ALLOWED_ENGINES = {"mock", "faster-whisper"}
_MAX_PROXY_LEN = 2048


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, ASR_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=ASR_SETTINGS_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def get_asr_settings(db: Session, defaults: SubtitleServiceSettings) -> dict[str, Any]:
    row = db.get(AppSetting, ASR_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    engine = str(stored.get("default_engine") or defaults.asr_engine).strip() or defaults.asr_engine
    if engine not in _ALLOWED_ENGINES:
        engine = defaults.asr_engine if defaults.asr_engine in _ALLOWED_ENGINES else "faster-whisper"

    language = str(stored.get("default_language") or "auto").strip() or "auto"
    model = str(stored.get("default_model") or defaults.whisper_model).strip() or defaults.whisper_model

    proxy = str(stored.get("model_download_proxy") or "").strip()
    if len(proxy) > _MAX_PROXY_LEN:
        proxy = proxy[:_MAX_PROXY_LEN]

    return {"default_engine": engine, "default_language": language, "default_model": model, "model_download_proxy": proxy}


def update_asr_settings(db: Session, defaults: SubtitleServiceSettings, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "default_engine" in update and update["default_engine"] is not None:
        val = str(update["default_engine"]).strip()
        if not val:
            stored.pop("default_engine", None)
        else:
            if val not in _ALLOWED_ENGINES:
                raise ValueError(f"default_engine must be one of: {sorted(_ALLOWED_ENGINES)}")
            stored["default_engine"] = val

    if "default_language" in update and update["default_language"] is not None:
        val = str(update["default_language"]).strip()
        if not val:
            stored.pop("default_language", None)
        else:
            stored["default_language"] = val

    if "default_model" in update and update["default_model"] is not None:
        val = str(update["default_model"]).strip()
        if not val:
            stored.pop("default_model", None)
        else:
            stored["default_model"] = val

    if "model_download_proxy" in update and update["model_download_proxy"] is not None:
        val = str(update["model_download_proxy"] or "").strip()
        if len(val) > _MAX_PROXY_LEN:
            raise ValueError(f"model_download_proxy is too long (max {_MAX_PROXY_LEN} chars)")
        if not val:
            stored.pop("model_download_proxy", None)
        else:
            stored["model_download_proxy"] = val

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_asr_settings(db, defaults)
