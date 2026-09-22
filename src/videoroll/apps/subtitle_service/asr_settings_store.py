from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.config import SubtitleServiceSettings
from videoroll.db.models import AppSetting
from videoroll.utils.fernet import decrypt_str, encrypt_str
from videoroll.utils.openai_compat import normalize_openai_base_url


ASR_SETTINGS_KEY = "subtitle.asr"

_ALLOWED_ENGINES = {"mock", "faster-whisper", "openvino", "external-whisper", "groq-whisper", "cloudflare-workers-ai"}
_MAX_PROXY_LEN = 2048
_MAX_EXTERNAL_BASE_URL_LEN = 2048
_MAX_EXTERNAL_MODEL_LEN = 256
_MAX_GROQ_MODEL_LEN = 256
_MAX_CLOUDFLARE_ACCOUNT_ID_LEN = 128
_MAX_CLOUDFLARE_MODEL_LEN = 256
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


def _decrypt_api_key(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    try:
        return decrypt_str(token).strip()
    except Exception:
        return ""


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
    elif engine == "external-whisper":
        engine_default_model = str(defaults.external_whisper_model or "").strip()
    elif engine == "groq-whisper":
        engine_default_model = str(getattr(defaults, "groq_whisper_model", "") or "whisper-large-v3-turbo").strip()
    elif engine == "cloudflare-workers-ai":
        engine_default_model = str(
            getattr(defaults, "cloudflare_workers_ai_model", "") or "@cf/openai/whisper-large-v3-turbo"
        ).strip()
    model = str(stored.get("default_model") or engine_default_model).strip() or engine_default_model
    if engine == "groq-whisper" and model not in {"whisper-large-v3", "whisper-large-v3-turbo"}:
        model = engine_default_model
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

    external = _as_dict(stored.get("external_whisper"))
    external_base_url = str(external.get("base_url") or defaults.external_whisper_base_url or "").strip()
    if external_base_url:
        external_base_url = normalize_openai_base_url(external_base_url)[:_MAX_EXTERNAL_BASE_URL_LEN]
    external_model = str(external.get("model") or defaults.external_whisper_model or "whisper-1").strip()[:_MAX_EXTERNAL_MODEL_LEN]
    external_api_key = _decrypt_api_key(external.get("api_key_enc")) or str(defaults.external_whisper_api_key or "").strip()
    external_batch_size = max(1, min(32, int(external.get("batch_size") or getattr(defaults, "external_whisper_batch_size", 1) or 1)))
    stored_external_vad_enabled = external.get("vad_enabled")
    external_vad_enabled = (
        bool(stored_external_vad_enabled)
        if isinstance(stored_external_vad_enabled, bool)
        else bool(getattr(defaults, "external_whisper_vad_enabled", True))
    )
    external_vad_threshold = max(0.1, min(0.95, float(external.get("vad_threshold") or getattr(defaults, "external_whisper_vad_threshold", 0.5) or 0.5)))
    external_min_silence_ms = max(50, min(5000, int(external.get("min_silence_ms") or getattr(defaults, "external_whisper_min_silence_ms", 500) or 500)))
    external_speech_pad_ms = max(0, min(2000, int(external.get("speech_pad_ms") if external.get("speech_pad_ms") is not None else getattr(defaults, "external_whisper_speech_pad_ms", 180))))
    stored_condition = external.get("condition_on_previous_text")
    external_condition_on_previous_text = (
        bool(stored_condition)
        if isinstance(stored_condition, bool)
        else bool(getattr(defaults, "external_whisper_condition_on_previous_text", False))
    )
    external_max_segment_seconds = max(1.0, min(30.0, float(external.get("max_segment_seconds") or getattr(defaults, "external_whisper_max_segment_seconds", 6.0) or 6.0)))
    external_max_segment_chars = max(10, min(500, int(external.get("max_segment_chars") or getattr(defaults, "external_whisper_max_segment_chars", 80) or 80)))

    groq = _as_dict(stored.get("groq_whisper"))
    groq_model = str(
        groq.get("model") or getattr(defaults, "groq_whisper_model", "") or "whisper-large-v3-turbo"
    ).strip()[:_MAX_GROQ_MODEL_LEN]
    groq_api_key = _decrypt_api_key(groq.get("api_key_enc")) or str(
        getattr(defaults, "groq_whisper_api_key", "") or ""
    ).strip()

    cloudflare = _as_dict(stored.get("cloudflare_workers_ai"))
    cloudflare_account_id = str(
        cloudflare.get("account_id") or getattr(defaults, "cloudflare_workers_ai_account_id", "") or ""
    ).strip()[:_MAX_CLOUDFLARE_ACCOUNT_ID_LEN]
    cloudflare_model = str(
        cloudflare.get("model")
        or getattr(defaults, "cloudflare_workers_ai_model", "")
        or "@cf/openai/whisper-large-v3-turbo"
    ).strip()[:_MAX_CLOUDFLARE_MODEL_LEN]
    cloudflare_api_key = _decrypt_api_key(cloudflare.get("api_key_enc")) or str(
        getattr(defaults, "cloudflare_workers_ai_api_key", "") or ""
    ).strip()

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
        "external_whisper_base_url": external_base_url,
        "external_whisper_model": external_model,
        "external_whisper_api_key": external_api_key,
        "external_whisper_api_key_set": bool(external_api_key),
        "external_whisper_batch_size": external_batch_size,
        "external_whisper_vad_enabled": external_vad_enabled,
        "external_whisper_vad_threshold": external_vad_threshold,
        "external_whisper_min_silence_ms": external_min_silence_ms,
        "external_whisper_speech_pad_ms": external_speech_pad_ms,
        "external_whisper_condition_on_previous_text": external_condition_on_previous_text,
        "external_whisper_max_segment_seconds": external_max_segment_seconds,
        "external_whisper_max_segment_chars": external_max_segment_chars,
        "groq_whisper_model": groq_model,
        "groq_whisper_api_key": groq_api_key,
        "groq_whisper_api_key_set": bool(groq_api_key),
        "cloudflare_workers_ai_account_id": cloudflare_account_id,
        "cloudflare_workers_ai_model": cloudflare_model,
        "cloudflare_workers_ai_api_key": cloudflare_api_key,
        "cloudflare_workers_ai_api_key_set": bool(cloudflare_api_key),
    }


def update_asr_settings(db: Session, defaults: SubtitleServiceSettings, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))
    external = dict(_as_dict(stored.get("external_whisper")))
    groq = dict(_as_dict(stored.get("groq_whisper")))
    cloudflare = dict(_as_dict(stored.get("cloudflare_workers_ai")))

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

    if "external_whisper_base_url" in update and update["external_whisper_base_url"] is not None:
        val = str(update["external_whisper_base_url"] or "").strip()
        if not val:
            external.pop("base_url", None)
        else:
            normalized = normalize_openai_base_url(val)
            if len(normalized) > _MAX_EXTERNAL_BASE_URL_LEN:
                raise ValueError(f"external_whisper_base_url is too long (max {_MAX_EXTERNAL_BASE_URL_LEN} chars)")
            external["base_url"] = normalized

    if "external_whisper_model" in update and update["external_whisper_model"] is not None:
        val = str(update["external_whisper_model"] or "").strip()
        if len(val) > _MAX_EXTERNAL_MODEL_LEN:
            raise ValueError(f"external_whisper_model is too long (max {_MAX_EXTERNAL_MODEL_LEN} chars)")
        if not val:
            external.pop("model", None)
        else:
            external["model"] = val

    if "external_whisper_api_key" in update and update["external_whisper_api_key"] is not None:
        val = str(update["external_whisper_api_key"] or "").strip()
        if val:
            external["api_key_enc"] = encrypt_str(val)
        else:
            external.pop("api_key_enc", None)

    if "external_whisper_batch_size" in update and update["external_whisper_batch_size"] is not None:
        external["batch_size"] = max(1, min(32, int(update["external_whisper_batch_size"])))
    if "external_whisper_vad_enabled" in update and update["external_whisper_vad_enabled"] is not None:
        external["vad_enabled"] = bool(update["external_whisper_vad_enabled"])
    if "external_whisper_vad_threshold" in update and update["external_whisper_vad_threshold"] is not None:
        external["vad_threshold"] = max(0.1, min(0.95, float(update["external_whisper_vad_threshold"])))
    if "external_whisper_min_silence_ms" in update and update["external_whisper_min_silence_ms"] is not None:
        external["min_silence_ms"] = max(50, min(5000, int(update["external_whisper_min_silence_ms"])))
    if "external_whisper_speech_pad_ms" in update and update["external_whisper_speech_pad_ms"] is not None:
        external["speech_pad_ms"] = max(0, min(2000, int(update["external_whisper_speech_pad_ms"])))
    if "external_whisper_condition_on_previous_text" in update and update["external_whisper_condition_on_previous_text"] is not None:
        external["condition_on_previous_text"] = bool(update["external_whisper_condition_on_previous_text"])
    if "external_whisper_max_segment_seconds" in update and update["external_whisper_max_segment_seconds"] is not None:
        external["max_segment_seconds"] = max(1.0, min(30.0, float(update["external_whisper_max_segment_seconds"])))
    if "external_whisper_max_segment_chars" in update and update["external_whisper_max_segment_chars"] is not None:
        external["max_segment_chars"] = max(10, min(500, int(update["external_whisper_max_segment_chars"])))

    if "groq_whisper_model" in update and update["groq_whisper_model"] is not None:
        val = str(update["groq_whisper_model"] or "").strip()
        if len(val) > _MAX_GROQ_MODEL_LEN:
            raise ValueError(f"groq_whisper_model is too long (max {_MAX_GROQ_MODEL_LEN} chars)")
        if not val:
            groq.pop("model", None)
        else:
            if val not in {"whisper-large-v3", "whisper-large-v3-turbo"}:
                raise ValueError("groq_whisper_model must be whisper-large-v3 or whisper-large-v3-turbo")
            groq["model"] = val

    if "groq_whisper_api_key" in update and update["groq_whisper_api_key"] is not None:
        val = str(update["groq_whisper_api_key"] or "").strip()
        if val:
            groq["api_key_enc"] = encrypt_str(val)
        else:
            groq.pop("api_key_enc", None)

    if "cloudflare_workers_ai_account_id" in update and update["cloudflare_workers_ai_account_id"] is not None:
        val = str(update["cloudflare_workers_ai_account_id"] or "").strip()
        if len(val) > _MAX_CLOUDFLARE_ACCOUNT_ID_LEN:
            raise ValueError(
                f"cloudflare_workers_ai_account_id is too long (max {_MAX_CLOUDFLARE_ACCOUNT_ID_LEN} chars)"
            )
        if not val:
            cloudflare.pop("account_id", None)
        else:
            cloudflare["account_id"] = val

    if "cloudflare_workers_ai_model" in update and update["cloudflare_workers_ai_model"] is not None:
        val = str(update["cloudflare_workers_ai_model"] or "").strip()
        if len(val) > _MAX_CLOUDFLARE_MODEL_LEN:
            raise ValueError(f"cloudflare_workers_ai_model is too long (max {_MAX_CLOUDFLARE_MODEL_LEN} chars)")
        if not val:
            cloudflare.pop("model", None)
        else:
            cloudflare["model"] = val

    if "cloudflare_workers_ai_api_key" in update and update["cloudflare_workers_ai_api_key"] is not None:
        val = str(update["cloudflare_workers_ai_api_key"] or "").strip()
        if val:
            cloudflare["api_key_enc"] = encrypt_str(val)
        else:
            cloudflare.pop("api_key_enc", None)

    if external:
        stored["external_whisper"] = external
    else:
        stored.pop("external_whisper", None)
    if groq:
        stored["groq_whisper"] = groq
    else:
        stored.pop("groq_whisper", None)
    if cloudflare:
        stored["cloudflare_workers_ai"] = cloudflare
    else:
        stored.pop("cloudflare_workers_ai", None)

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_asr_settings(db, defaults)
