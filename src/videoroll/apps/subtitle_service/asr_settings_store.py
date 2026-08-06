from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.config import SubtitleServiceSettings
from videoroll.db.models import AppSetting


ASR_SETTINGS_KEY = "subtitle.asr"

_ALLOWED_ENGINES = {"mock", "faster-whisper", "openvino"}
_MAX_PROXY_LEN = 2048
_MIN_OPENVINO_VAD_THRESHOLD = 0.1
_MAX_OPENVINO_VAD_THRESHOLD = 0.95


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
    engine_default_model = defaults.whisper_model
    if engine == "openvino":
        engine_default_model = str(defaults.openvino_model or "").strip()
    model = str(stored.get("default_model") or engine_default_model).strip() or engine_default_model
    openvino_device = str(stored.get("openvino_device") or defaults.openvino_device).strip() or defaults.openvino_device
    openvino_num_beams = int(stored.get("openvino_num_beams") or defaults.openvino_num_beams or 1)
    if openvino_num_beams <= 0:
        openvino_num_beams = int(defaults.openvino_num_beams or 1) or 1
    openvino_max_new_tokens = int(stored.get("openvino_max_new_tokens") or defaults.openvino_max_new_tokens or 448)
    if openvino_max_new_tokens <= 0:
        openvino_max_new_tokens = int(defaults.openvino_max_new_tokens or 448) or 448
    stored_vad_enabled = stored.get("openvino_vad_enabled")
    openvino_vad_enabled = (
        bool(stored_vad_enabled)
        if isinstance(stored_vad_enabled, bool)
        else bool(defaults.openvino_vad_enabled)
    )
    try:
        openvino_vad_threshold = float(stored.get("openvino_vad_threshold") or defaults.openvino_vad_threshold or 0.5)
    except (TypeError, ValueError):
        openvino_vad_threshold = float(defaults.openvino_vad_threshold or 0.5)
    if not _MIN_OPENVINO_VAD_THRESHOLD <= openvino_vad_threshold <= _MAX_OPENVINO_VAD_THRESHOLD:
        openvino_vad_threshold = float(defaults.openvino_vad_threshold or 0.5)

    proxy = str(stored.get("model_download_proxy") or "").strip()
    if len(proxy) > _MAX_PROXY_LEN:
        proxy = proxy[:_MAX_PROXY_LEN]

    return {
        "default_engine": engine,
        "default_language": language,
        "default_model": model,
        "openvino_device": openvino_device,
        "openvino_num_beams": openvino_num_beams,
        "openvino_max_new_tokens": openvino_max_new_tokens,
        "openvino_vad_enabled": openvino_vad_enabled,
        "openvino_vad_threshold": openvino_vad_threshold,
        "model_download_proxy": proxy,
    }


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

    if "openvino_device" in update and update["openvino_device"] is not None:
        val = str(update["openvino_device"]).strip()
        if not val:
            stored.pop("openvino_device", None)
        else:
            stored["openvino_device"] = val

    if "openvino_num_beams" in update and update["openvino_num_beams"] is not None:
        val = int(update["openvino_num_beams"])
        if val <= 0:
            raise ValueError("openvino_num_beams must be >= 1")
        stored["openvino_num_beams"] = val

    if "openvino_max_new_tokens" in update and update["openvino_max_new_tokens"] is not None:
        val = int(update["openvino_max_new_tokens"])
        if val <= 0:
            raise ValueError("openvino_max_new_tokens must be >= 1")
        stored["openvino_max_new_tokens"] = val

    if "openvino_vad_enabled" in update and update["openvino_vad_enabled"] is not None:
        stored["openvino_vad_enabled"] = bool(update["openvino_vad_enabled"])

    if "openvino_vad_threshold" in update and update["openvino_vad_threshold"] is not None:
        val = float(update["openvino_vad_threshold"])
        if not _MIN_OPENVINO_VAD_THRESHOLD <= val <= _MAX_OPENVINO_VAD_THRESHOLD:
            raise ValueError(
                f"openvino_vad_threshold must be between {_MIN_OPENVINO_VAD_THRESHOLD} and {_MAX_OPENVINO_VAD_THRESHOLD}"
            )
        stored["openvino_vad_threshold"] = val

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
