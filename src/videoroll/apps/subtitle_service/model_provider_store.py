"""Persistent OpenAI-compatible provider catalog for subtitle translation.

Secrets never leave the server. The virtual "legacy" provider preserves existing
installations without changing their historical OpenAI settings.
"""
from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting
from videoroll.utils.fernet import decrypt_str, encrypt_str
from videoroll.utils.openai_compat import normalize_openai_base_url

KEY = "subtitle.model_providers"
LEGACY_ID = "legacy"
INDEX_ID = "index_translate"

# JavaScript Number cannot round-trip larger integers through JSON exactly.
# This is a wire-format precision boundary, not a model-specific token cap.
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


def validate_token_limits(context: int | None, output: int | None) -> None:
    """Validate exact, positive token counts without a model-specific ceiling."""
    for label, value in (("上下文窗口", context), ("最大输出 Token", output)):
        if value is None:
            continue
        if type(value) is not int or not 1 <= value <= MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{label}必须是正整数且不超过 JSON 安全整数范围")
    if context is not None and output is not None and output >= context:
        raise ValueError("最大输出 Token 必须小于上下文窗口")




def _rows(db: Session) -> list[dict[str, Any]]:
    row = db.get(AppSetting, KEY)
    value = row.value_json if row else {}
    return list(value.get("providers", [])) if isinstance(value, dict) else []


def _safe(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "name": item["name"],
        "base_url": item["base_url"],
        "api_type": item.get("api_type", "openai"),
        "api_key_set": bool(item.get("api_key_enc")),
        "models": list(item.get("models", [])),
        "timeout_seconds": item.get("timeout_seconds", 180),
        "max_retries": item.get("max_retries", 3),
    }


def list_providers(db: Session, legacy: dict[str, Any]) -> list[dict[str, Any]]:
    virtual = {
        "id": LEGACY_ID,
        "name": "现有 OpenAI 配置",
        "base_url": legacy["openai_base_url"],
        "api_type": legacy.get("openai_api_type", "openai"),
        "api_key_set": bool(legacy.get("openai_api_key_set")),
        "models": [{"id": legacy["openai_model"], "context_window": None,
                    "max_output_tokens": None, "temperature": legacy.get("openai_temperature", 0.2)}],
        "timeout_seconds": legacy.get("openai_timeout_seconds", 180),
        "max_retries": legacy.get("openai_max_retries", 3),
    }
    index_provider = {
        "id": INDEX_ID,
        "name": "Index-Translate（官方免费）",
        "base_url": "https://index-translate.bilibili.com/v1",
        "api_type": "index_translate",
        "api_key_set": False,
        "models": [{
            "id": "Index-Translate-35B-A3B",
            "context_window": None,
            "max_output_tokens": int(legacy.get("index_max_tokens") or 4096),
            "temperature": float(legacy.get("index_temperature") if legacy.get("index_temperature") is not None else 0),
            "enable_thinking": False,
            "reasoning_effort": "medium",
        }],
        "timeout_seconds": legacy.get("index_timeout_seconds", 120),
        "max_retries": legacy.get("default_max_retries", 2),
    }
    return [virtual, index_provider] + [_safe(row) for row in _rows(db)]


def _model(item: dict[str, Any]) -> dict[str, Any]:
    model_id = str(item.get("id") or "").strip()
    if not model_id or len(model_id) > 256:
        raise ValueError("模型 ID 不能为空，且最长 256 字符")
    context = item.get("context_window")
    output = item.get("max_output_tokens")
    temperature = item.get("temperature", 0.2)
    validate_token_limits(context, output)
    if temperature is not None and (not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2):
        raise ValueError("温度必须在 0 到 2 之间")
    if not isinstance(item.get("enable_thinking", False), bool):
        raise ValueError("Thinking 参数必须是布尔值")
    effort = str(item.get("reasoning_effort") or "medium")
    if effort not in {"low", "medium", "high"}:
        raise ValueError("推理强度只能是 low、medium 或 high")
    return {"id": model_id, "context_window": context,
            "max_output_tokens": output, "temperature": temperature,
            "enable_thinking": bool(item.get("enable_thinking", False)),
            "reasoning_effort": effort}


def save_provider(db: Session, provider_id: str | None, values: dict[str, Any]) -> dict[str, Any]:
    provider_id = provider_id or str(uuid.uuid4())
    if provider_id in {LEGACY_ID, INDEX_ID}:
        raise ValueError("内置提供方应使用翻译高级设置修改")
    original = _rows(db)
    previous = next((item for item in original if item["id"] == provider_id), {})
    name = str(values.get("name") or "").strip()[:80]
    base_url = normalize_openai_base_url(str(values.get("base_url") or "").strip())
    scheme = urlsplit(base_url)
    if not name or scheme.scheme not in {"http", "https"} or not scheme.hostname or scheme.username or scheme.password:
        raise ValueError("提供方名称与 HTTP(S) API 地址必须有效")
    api_type = str(values.get("api_type") or "openai")
    if api_type not in {"openai", "cerebras"}:
        raise ValueError("不支持的接口类型")
    models = [_model(item) for item in values.get("models", [])]
    if len(models) > 200 or len({x["id"] for x in models}) != len(models):
        raise ValueError("模型不能重复且最多 200 个")
    key = values.get("api_key")
    token = previous.get("api_key_enc", "")
    if key is not None:
        token = encrypt_str(str(key)) if str(key) else ""
    timeout = int(values.get("timeout_seconds") or 180)
    retries = int(values.get("max_retries") or 3)
    if not 5 <= timeout <= 1200 or not 1 <= retries <= 10:
        raise ValueError("超时或重试次数超出允许范围")
    item = {"id": provider_id, "name": name, "base_url": base_url, "api_type": api_type,
            "api_key_enc": token, "models": models, "timeout_seconds": timeout, "max_retries": retries}
    new_rows = [row for row in original if row["id"] != provider_id] + [item]
    row = db.get(AppSetting, KEY)
    if row is None:
        row = AppSetting(key=KEY, value_json={})
        db.add(row)
    row.value_json = {"providers": new_rows}
    db.commit()
    return _safe(item)


def delete_provider(db: Session, provider_id: str) -> None:
    if provider_id in {LEGACY_ID, INDEX_ID}:
        raise ValueError("内置提供方不能删除")
    rows = _rows(db)
    if not any(item["id"] == provider_id for item in rows):
        raise ValueError("提供方不存在")
    row = db.get(AppSetting, KEY)
    assert row is not None
    row.value_json = {"providers": [item for item in rows if item["id"] != provider_id]}
    db.commit()


def resolve_provider_settings(
    db: Session, legacy: dict[str, Any], provider_id: str | None, model_id: str | None,
) -> dict[str, Any]:
    """Resolve one task's model, failing closed for removed/renamed selections."""
    provider_id = provider_id or LEGACY_ID
    model_id = (model_id or "").strip()
    result = dict(legacy)
    if provider_id == LEGACY_ID:
        if model_id and model_id != legacy["openai_model"]:
            raise ValueError("原有配置中的模型与当前选择不一致，请重新选择")
        return result
    provider = next((item for item in _rows(db) if item["id"] == provider_id), None)
    if provider is None:
        raise ValueError(f"模型提供方已删除或不存在：{provider_id}")
    model = next((item for item in provider["models"] if item["id"] == model_id), None)
    if model is None:
        raise ValueError("模型已删除或不存在，请在任务设置中重新选择")
    result.update({
        # Local Ollama / llama.cpp endpoints may not require a secret. The
        # OpenAI client still expects a nonempty Bearer token.
        "openai_api_key": decrypt_str(provider["api_key_enc"]) if provider.get("api_key_enc") else "local-model",
        "openai_api_key_set": bool(provider.get("api_key_enc")),
        "openai_base_url": provider["base_url"],
        "openai_model": model_id,
        "openai_api_type": provider.get("api_type", "openai"),
        "openai_temperature": model.get("temperature") if model.get("temperature") is not None else legacy["openai_temperature"],
        "openai_timeout_seconds": provider.get("timeout_seconds", 180),
        "openai_max_retries": provider.get("max_retries", 3),
        "openai_enable_thinking": bool(model.get("enable_thinking", False)),
        "cerebras_reasoning_effort": model.get("reasoning_effort", "medium"),
        "model_context_window": model.get("context_window"),
        "max_output_tokens": model.get("max_output_tokens"),
    })
    return result


def _remote_token_limit(value: object) -> int | None:
    # Only trust server-reported positive integer metadata, never model names.
    if type(value) is int and 1 <= value <= MAX_SAFE_JSON_INTEGER:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 16:
        number = int(value)
        if 1 <= number <= MAX_SAFE_JSON_INTEGER:
            return number
    return None


def _remote_model_details(item: dict[str, Any]) -> dict[str, int | None]:
    """Read documented extension fields from /models when a gateway supplies them.

    Standard OpenAI /v1/models does not promise context/output metadata.
    OpenRouter exposes it in top_provider; other gateways may expose root keys.
    Never fill unknown fields from a model name, heuristic, or a global default.
    """
    top = item.get("top_provider")
    provider_metadata = top if isinstance(top, dict) else {}
    context = next((
        limit for limit in (
            _remote_token_limit(provider_metadata.get("context_length")),
            _remote_token_limit(item.get("context_length")),
            _remote_token_limit(item.get("context_window")),
            _remote_token_limit(item.get("max_model_len")),
        ) if limit is not None
    ), None)
    output = next((
        limit for limit in (
            _remote_token_limit(provider_metadata.get("max_completion_tokens")),
            _remote_token_limit(item.get("max_completion_tokens")),
            _remote_token_limit(item.get("max_output_tokens")),
        ) if limit is not None
    ), None)
    # Invalid or contradictory third-party metadata must not be imported.
    if context is not None and output is not None and output >= context:
        output = None
    return {"context_window": context, "max_output_tokens": output}


def fetch_remote_model_catalog(db: Session, provider_id: str) -> dict[str, Any]:
    """Discover model IDs and optional published limits without saving overrides."""
    provider = next((row for row in _rows(db) if row["id"] == provider_id), None)
    if provider is None:
        raise ValueError("请先保存新提供方再获取模型")
    base = provider["base_url"].rstrip("/")
    url = base + ("/models" if base.endswith("/v1") else "/v1/models")
    headers = {}
    if provider.get("api_key_enc"):
        headers["Authorization"] = "Bearer " + decrypt_str(provider["api_key_enc"])
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False, trust_env=False) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("接口未返回 OpenAI /models 格式")

    discovered: dict[str, dict[str, int | None]] = {}
    for item in data["data"][:500]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not 0 < len(model_id) <= 256:
            continue
        if model_id not in discovered:
            discovered[model_id] = _remote_model_details(item)
    names = sorted(discovered)[:200]
    return {"models": names, "details": {
        name: discovered[name] for name in names
        if discovered[name]["context_window"] is not None
        or discovered[name]["max_output_tokens"] is not None
    }}


def fetch_remote_models(db: Session, provider_id: str) -> list[str]:
    """Legacy helper for callers only interested in model IDs."""
    return fetch_remote_model_catalog(db, provider_id)["models"]
