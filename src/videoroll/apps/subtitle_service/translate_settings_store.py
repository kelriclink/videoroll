from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.config import SubtitleServiceSettings
from videoroll.db.models import AppSetting
from videoroll.utils.fernet import decrypt_str, encrypt_str
from videoroll.utils.openai_compat import normalize_openai_base_url


TRANSLATE_SETTINGS_KEY = "subtitle.translate"
_SEARXNG_TIME_RANGES = {"", "day", "month", "year"}


def _clean_csv(value: Any, *, default: str = "", limit: int = 20) -> str:
    raw_items = str(value or default or "").replace("\n", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        clean = " ".join(str(item or "").strip().split())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean[:80])
        if len(out) >= limit:
            break
    return ",".join(out)


def _clean_search_language(value: Any, *, default: str = "all") -> str:
    clean = str(value or default or "all").strip()
    if not clean:
        return "all"
    return clean[:32]


def _clean_search_time_range(value: Any) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in _SEARXNG_TIME_RANGES else ""


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
    embedding_openai = _as_dict(stored.get("rag_embedding_openai"))
    embedding_api_key = ""
    embedding_api_key_enc = embedding_openai.get("api_key_enc")
    if isinstance(embedding_api_key_enc, str) and embedding_api_key_enc.strip():
        try:
            embedding_api_key = decrypt_str(embedding_api_key_enc)
        except Exception:
            embedding_api_key = ""
    default_embedding_base_url = str(defaults.rag_embedding_base_url or "").strip()

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
        "openai_max_retries": int(openai.get("max_retries") or stored.get("openai_max_retries") or 3),
        "rag_enabled": bool(stored.get("rag_enabled") if "rag_enabled" in stored else defaults.rag_enabled),
        "rag_top_k": int(stored.get("rag_top_k") or defaults.rag_top_k),
        "rag_min_score": float(stored.get("rag_min_score") or defaults.rag_min_score),
        "rag_embedding_provider": str(stored.get("rag_embedding_provider") or defaults.rag_embedding_provider),
        "rag_embedding_model": str(stored.get("rag_embedding_model") or defaults.rag_embedding_model),
        "rag_embedding_dimensions": int(stored.get("rag_embedding_dimensions") or defaults.rag_embedding_dimensions),
        "rag_embedding_model_dir": str(stored.get("rag_embedding_model_dir") or defaults.rag_embedding_model_dir),
        "rag_embedding_device": str(stored.get("rag_embedding_device") or defaults.rag_embedding_device),
        "rag_embedding_api_key": embedding_api_key or str(defaults.rag_embedding_api_key or ""),
        "rag_embedding_api_key_set": bool(embedding_api_key),
        "rag_embedding_base_url": normalize_openai_base_url(str(embedding_openai.get("base_url") or default_embedding_base_url)),
        "rag_embedding_timeout_seconds": float(
            embedding_openai.get("timeout_seconds")
            or stored.get("rag_embedding_timeout_seconds")
            or defaults.rag_embedding_timeout_seconds
        ),
        "rag_auto_discover_terms": bool(
            stored.get("rag_auto_discover_terms") if "rag_auto_discover_terms" in stored else defaults.rag_auto_discover_terms
        ),
        "rag_auto_learn_terms": bool(stored.get("rag_auto_learn_terms") if "rag_auto_learn_terms" in stored else defaults.rag_auto_learn_terms),
        "rag_dictionary_enabled": bool(
            stored.get("rag_dictionary_enabled")
            if "rag_dictionary_enabled" in stored
            else getattr(defaults, "rag_dictionary_enabled", True)
        ),
        "rag_dictionary_top_k": int(stored.get("rag_dictionary_top_k") or getattr(defaults, "rag_dictionary_top_k", 8)),
        "rag_dictionary_min_quality": float(
            stored.get("rag_dictionary_min_quality")
            if stored.get("rag_dictionary_min_quality") is not None
            else getattr(defaults, "rag_dictionary_min_quality", 0.0)
        ),
        "rag_dictionary_auto_promote": bool(
            stored.get("rag_dictionary_auto_promote")
            if "rag_dictionary_auto_promote" in stored
            else getattr(defaults, "rag_dictionary_auto_promote", False)
        ),
        "rag_wiki_enabled": bool(stored.get("rag_wiki_enabled") if "rag_wiki_enabled" in stored else False),
        "rag_search_enabled": bool(stored.get("rag_search_enabled") if "rag_search_enabled" in stored else defaults.rag_search_enabled),
        "rag_search_url": str(stored.get("rag_search_url") or defaults.rag_search_url),
        "rag_search_categories": _clean_csv(stored.get("rag_search_categories"), default=defaults.rag_search_categories or "general"),
        "rag_search_engines": _clean_csv(stored.get("rag_search_engines"), default=defaults.rag_search_engines or ""),
        "rag_search_fallback_engines": _clean_csv(
            stored.get("rag_search_fallback_engines"),
            default=defaults.rag_search_fallback_engines or "bing,baidu",
        ),
        "rag_search_language": _clean_search_language(stored.get("rag_search_language"), default=defaults.rag_search_language or "all"),
        "rag_search_safesearch": max(
            0,
            min(
                2,
                int(
                    stored.get("rag_search_safesearch")
                    if stored.get("rag_search_safesearch") is not None
                    else defaults.rag_search_safesearch
                ),
            ),
        ),
        "rag_search_time_range": _clean_search_time_range(stored.get("rag_search_time_range") or defaults.rag_search_time_range),
        "rag_search_pageno": max(1, min(100, int(stored.get("rag_search_pageno") or defaults.rag_search_pageno or 1))),
        "rag_domain": str(stored.get("rag_domain") or defaults.rag_domain),
        "rag_agent_parallelism": int(stored.get("rag_agent_parallelism") or 1),
        "rag_agent_timeout_seconds": float(stored.get("rag_agent_timeout_seconds") or 120.0),
        "rag_agent_skills_enabled": bool(
            stored.get("rag_agent_skills_enabled")
            if "rag_agent_skills_enabled" in stored
            else getattr(defaults, "rag_agent_skills_enabled", False)
        ),
        "rag_agent_builtin_skills_enabled": bool(
            stored.get("rag_agent_builtin_skills_enabled")
            if "rag_agent_builtin_skills_enabled" in stored
            else getattr(defaults, "rag_agent_builtin_skills_enabled", True)
        ),
        "rag_agent_user_skills_enabled": bool(
            stored.get("rag_agent_user_skills_enabled")
            if "rag_agent_user_skills_enabled" in stored
            else getattr(defaults, "rag_agent_user_skills_enabled", True)
        ),
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
        "rag_embedding_timeout_seconds",
        "rag_auto_discover_terms",
        "rag_auto_learn_terms",
        "rag_dictionary_enabled",
        "rag_dictionary_top_k",
        "rag_dictionary_min_quality",
        "rag_dictionary_auto_promote",
        "rag_wiki_enabled",
        "rag_search_enabled",
        "rag_search_url",
        "rag_search_categories",
        "rag_search_engines",
        "rag_search_fallback_engines",
        "rag_search_language",
        "rag_search_safesearch",
        "rag_search_time_range",
        "rag_search_pageno",
        "rag_domain",
        "rag_agent_parallelism",
        "rag_agent_timeout_seconds",
        "rag_agent_skills_enabled",
        "rag_agent_builtin_skills_enabled",
        "rag_agent_user_skills_enabled",
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
        ("openai_max_retries", "max_retries"),
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

    embedding_openai = dict(_as_dict(stored.get("rag_embedding_openai")))
    if "rag_embedding_api_key" in update:
        key = update.get("rag_embedding_api_key")
        if key is not None:
            key = str(key).strip()
            if not key:
                embedding_openai.pop("api_key_enc", None)
            else:
                embedding_openai["api_key_enc"] = encrypt_str(key)
    if "rag_embedding_base_url" in update and update.get("rag_embedding_base_url") is not None:
        embedding_openai["base_url"] = normalize_openai_base_url(str(update.get("rag_embedding_base_url") or ""))
    if "rag_embedding_timeout_seconds" in update and update.get("rag_embedding_timeout_seconds") is not None:
        embedding_openai["timeout_seconds"] = update.get("rag_embedding_timeout_seconds")
    stored["rag_embedding_openai"] = embedding_openai

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
    try:
        stored["rag_agent_parallelism"] = max(1, min(8, int(stored.get("rag_agent_parallelism") or 1)))
    except Exception:
        stored["rag_agent_parallelism"] = 1
    try:
        stored["rag_agent_timeout_seconds"] = max(10.0, min(900.0, float(stored.get("rag_agent_timeout_seconds") or 120.0)))
    except Exception:
        stored["rag_agent_timeout_seconds"] = 120.0
    try:
        stored["rag_dictionary_top_k"] = max(0, min(30, int(stored.get("rag_dictionary_top_k") or getattr(defaults, "rag_dictionary_top_k", 8))))
    except Exception:
        stored["rag_dictionary_top_k"] = getattr(defaults, "rag_dictionary_top_k", 8)
    try:
        stored["rag_dictionary_min_quality"] = max(
            0.0,
            min(
                1.0,
                float(
                    stored.get("rag_dictionary_min_quality")
                    if stored.get("rag_dictionary_min_quality") is not None
                    else getattr(defaults, "rag_dictionary_min_quality", 0.0)
                ),
            ),
        )
    except Exception:
        stored["rag_dictionary_min_quality"] = getattr(defaults, "rag_dictionary_min_quality", 0.0)
    try:
        stored["rag_embedding_timeout_seconds"] = max(
            1.0,
            min(600.0, float(stored.get("rag_embedding_timeout_seconds") or defaults.rag_embedding_timeout_seconds)),
        )
    except Exception:
        stored["rag_embedding_timeout_seconds"] = defaults.rag_embedding_timeout_seconds
    for bool_key in [
        "rag_enabled",
        "rag_auto_discover_terms",
        "rag_auto_learn_terms",
        "rag_dictionary_enabled",
        "rag_dictionary_auto_promote",
        "rag_wiki_enabled",
        "rag_search_enabled",
        "rag_agent_skills_enabled",
        "rag_agent_builtin_skills_enabled",
        "rag_agent_user_skills_enabled",
    ]:
        if bool_key in stored:
            stored[bool_key] = bool(stored[bool_key])
    provider = str(stored.get("rag_embedding_provider") or defaults.rag_embedding_provider).strip().lower()
    stored["rag_embedding_provider"] = provider if provider in {"openai", "local"} else defaults.rag_embedding_provider
    for str_key in [
        "rag_embedding_model",
        "rag_embedding_model_dir",
        "rag_embedding_device",
        "rag_search_url",
        "rag_search_language",
        "rag_search_time_range",
        "rag_domain",
    ]:
        if str_key in stored and stored[str_key] is not None:
            stored[str_key] = str(stored[str_key]).strip()
    stored["rag_search_categories"] = _clean_csv(stored.get("rag_search_categories"), default=defaults.rag_search_categories or "general")
    stored["rag_search_engines"] = _clean_csv(stored.get("rag_search_engines"), default=defaults.rag_search_engines or "")
    stored["rag_search_fallback_engines"] = _clean_csv(
        stored.get("rag_search_fallback_engines"),
        default=defaults.rag_search_fallback_engines or "bing,baidu",
    )
    stored["rag_search_language"] = _clean_search_language(stored.get("rag_search_language"), default=defaults.rag_search_language or "all")
    try:
        stored["rag_search_safesearch"] = max(0, min(2, int(stored.get("rag_search_safesearch") or defaults.rag_search_safesearch or 0)))
    except Exception:
        stored["rag_search_safesearch"] = 0
    stored["rag_search_time_range"] = _clean_search_time_range(stored.get("rag_search_time_range") or defaults.rag_search_time_range)
    try:
        stored["rag_search_pageno"] = max(1, min(100, int(stored.get("rag_search_pageno") or defaults.rag_search_pageno or 1)))
    except Exception:
        stored["rag_search_pageno"] = 1
    if "base_url" in embedding_openai:
        embedding_openai["base_url"] = normalize_openai_base_url(str(embedding_openai.get("base_url") or ""))
    try:
        if "timeout_seconds" in embedding_openai and embedding_openai["timeout_seconds"] is not None:
            embedding_openai["timeout_seconds"] = max(1.0, min(600.0, float(embedding_openai.get("timeout_seconds"))))
    except Exception:
        embedding_openai["timeout_seconds"] = defaults.rag_embedding_timeout_seconds
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
    try:
        if "max_retries" in openai and openai["max_retries"] is not None:
            openai["max_retries"] = max(1, min(10, int(openai.get("max_retries") or 3)))
    except Exception:
        openai["max_retries"] = 3

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_translate_settings(db, defaults)
