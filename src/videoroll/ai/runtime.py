from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from videoroll.ai.client import OpenAIChatConfig, openai_chat_config_from_settings


AISettingsProvider = Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class AIRuntime:
    purpose: str
    provider: str
    config: OpenAIChatConfig
    settings: Mapping[str, Any]


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _purpose_overrides(settings: Mapping[str, Any], purpose: str) -> Mapping[str, Any]:
    raw = settings.get("ai_purpose_overrides") or settings.get("purpose_overrides") or settings.get("purposes")
    overrides = _as_mapping(raw)
    exact = _as_mapping(overrides.get(purpose))
    if exact:
        return exact
    fallback = _as_mapping(overrides.get("*"))
    return fallback


def _settings_for_purpose(settings: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    merged = dict(settings)
    merged.update(dict(_purpose_overrides(settings, purpose)))
    return merged


def _normalize_chat_provider(settings: Mapping[str, Any]) -> str:
    raw = str(settings.get("ai_provider") or settings.get("chat_provider") or "openai").strip().lower()
    if raw in {"openai", "openai_compatible", "openai-compatible"}:
        return "openai"
    return raw


class AIRuntimeResolver:
    def __init__(self, settings_provider: AISettingsProvider) -> None:
        self._settings_provider = settings_provider

    def current_settings(self) -> Mapping[str, Any]:
        settings = self._settings_provider()
        return settings if isinstance(settings, Mapping) else {}

    def resolve(self, purpose: str = "chat") -> AIRuntime:
        clean_purpose = str(purpose or "chat").strip() or "chat"
        settings = _settings_for_purpose(self.current_settings(), clean_purpose)
        provider = _normalize_chat_provider(settings)
        config = openai_chat_config_from_settings(settings)
        if provider == "openai":
            if not config.api_key:
                raise RuntimeError("OpenAI API key is not set")
            if not config.model:
                raise RuntimeError("OpenAI model is not set")
        return AIRuntime(purpose=clean_purpose, provider=provider, config=config, settings=settings)
