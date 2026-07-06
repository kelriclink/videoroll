from __future__ import annotations

from typing import Any, Protocol

from videoroll.ai.client import request_openai_json_object
from videoroll.ai.prompts import AIJsonPrompt
from videoroll.ai.runtime import AIRuntime


class AIProvider(Protocol):
    name: str

    def request_json(self, runtime: AIRuntime, prompt: AIJsonPrompt, *, client: Any | None = None) -> dict[str, Any]:
        ...


class OpenAICompatibleProvider:
    name = "openai"

    def request_json(self, runtime: AIRuntime, prompt: AIJsonPrompt, *, client: Any | None = None) -> dict[str, Any]:
        return request_openai_json_object(
            config=runtime.config,
            system_prompt=prompt.system_prompt,
            user_prompt=prompt.user_prompt,
            format_retry_notice=prompt.format_retry_notice,
            format_retries=prompt.format_retries,
            network_retries=prompt.network_retries,
            client=client,
        )


class AIProviderRegistry:
    def __init__(self, providers: list[AIProvider] | None = None) -> None:
        providers = providers or [OpenAICompatibleProvider()]
        self._providers = {provider.name: provider for provider in providers}

    def get(self, name: str) -> AIProvider:
        clean = str(name or "").strip().lower()
        provider = self._providers.get(clean)
        if provider is None:
            raise RuntimeError(f"unsupported AI provider: {clean}")
        return provider
