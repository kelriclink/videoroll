from __future__ import annotations

import time
from typing import Any, Callable

from videoroll.ai.client import OpenAIChatConfig, request_openai_json_object, request_openai_json_object_with_thinking
from videoroll.ai.prompts import (
    AIJsonPrompt,
    build_bilibili_tags_prompt,
    build_publish_review_prompt,
    build_subtitle_repair_prompt,
    build_subtitle_translation_prompt,
    build_title_translation_prompt,
    build_text_translation_prompt,
    build_typeid_prompt,
)
from videoroll.ai.providers import AIProviderRegistry
from videoroll.ai.runtime import AIRuntime, AIRuntimeResolver, AISettingsProvider


class AIService:
    """
    Use-case oriented AI facade.

    Business code calls methods such as translate_subtitle_batch() or
    review_publish_content(). Runtime resolution, provider selection, and
    transport calls stay behind this object.
    """

    def __init__(
        self,
        settings_provider: AISettingsProvider,
        *,
        runtime_resolver: AIRuntimeResolver | None = None,
        provider_registry: AIProviderRegistry | None = None,
    ) -> None:
        self._runtime_resolver = runtime_resolver or AIRuntimeResolver(settings_provider)
        self._providers = provider_registry or AIProviderRegistry()

    def current_settings(self) -> Any:
        return self._runtime_resolver.current_settings()

    def resolve_current_runtime(self, purpose: str = "chat") -> AIRuntime:
        return self._runtime_resolver.resolve(purpose)

    def request_json(
        self,
        *,
        purpose: str,
        system_prompt: str,
        user_prompt: str,
        format_retry_notice: str = "注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。",
        format_retries: int = 2,
        network_retries: int | None = None,
        client: Any | None = None,
        enable_thinking: bool = False,
        on_thinking_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        prompt = AIJsonPrompt(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            format_retry_notice=format_retry_notice,
            format_retries=format_retries,
            network_retries=network_retries,
        )
        return self._request_json_prompt(
            purpose,
            prompt,
            client=client,
            enable_thinking=enable_thinking,
            on_thinking_delta=on_thinking_delta,
        )

    def _request_json_prompt(
        self,
        purpose: str,
        prompt: AIJsonPrompt,
        *,
        client: Any | None = None,
        enable_thinking: bool = False,
        on_thinking_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        runtime = self.resolve_current_runtime(purpose)
        if enable_thinking:
            if runtime.provider != "openai":
                raise RuntimeError(f"Think mode is only supported by OpenAI-compatible providers (got {runtime.provider})")
            return request_openai_json_object_with_thinking(
                config=runtime.config,
                system_prompt=prompt.system_prompt,
                user_prompt=prompt.user_prompt,
                format_retry_notice=prompt.format_retry_notice,
                format_retries=prompt.format_retries,
                network_retries=prompt.network_retries,
                client=client,
                on_thinking_delta=on_thinking_delta,
            )
        provider = self._providers.get(runtime.provider)
        return provider.request_json(runtime, prompt, client=client)

    def translate_text(
        self,
        text: str,
        *,
        target_lang: str,
        style: str,
        enable_thinking: bool = False,
        on_thinking_delta: Callable[[str], None] | None = None,
    ) -> str:
        source = str(text or "").strip()
        if not source:
            return text
        data = self._request_json_prompt(
            "text_translation",
            build_text_translation_prompt(source, target_lang=target_lang, style=style),
            enable_thinking=enable_thinking,
            on_thinking_delta=on_thinking_delta,
        )
        translated = str(data.get("translation") or "").strip()
        return translated or source

    def translate_title(
        self,
        title: str,
        *,
        target_lang: str,
        style: str,
        summary: str = "",
        retry_count: int = 4,
        retry_delay_seconds: float = 1.0,
    ) -> str:
        source = str(title or "").strip()
        if not source:
            return title
        prompt = build_title_translation_prompt(
            source,
            target_lang=target_lang,
            style=style,
            summary=summary,
        )
        attempts = max(1, min(10, int(retry_count) + 1))
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                data = self._request_json_prompt("title_translation", prompt)
                translated = str(data.get("translation") or "").strip()
                if not translated:
                    raise RuntimeError("OpenAI title translation output is empty")
                return translated
            except Exception as error:
                last_error = error
                if attempt >= attempts - 1:
                    break
                delay = max(0.0, float(retry_delay_seconds)) * min(8.0, float(2**attempt))
                if delay > 0:
                    time.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("OpenAI title translation failed")

    def translate_subtitle_batch(
        self,
        *,
        blocks: list[dict[str, Any]],
        target_lang: str,
        style: str,
        summary: str = "",
        enable_summary: bool = True,
        glossary: dict[str, str] | None = None,
        rag_context: dict[str, Any] | None = None,
        translation_plan: dict[str, Any] | None = None,
        network_retries: int = 3,
        enable_thinking: bool = False,
        on_thinking_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        return self._request_json_prompt(
            "subtitle_translation",
            build_subtitle_translation_prompt(
                blocks=blocks,
                target_lang=target_lang,
                style=style,
                summary=summary,
                enable_summary=enable_summary,
                glossary=glossary,
                rag_context=rag_context,
                translation_plan=translation_plan,
                network_retries=network_retries,
            ),
            enable_thinking=enable_thinking,
            on_thinking_delta=on_thinking_delta,
        )

    def repair_subtitle_batch(
        self,
        *,
        source_blocks: list[dict[str, Any]],
        draft_translations: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        target_lang: str,
        style: str,
        translation_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json_prompt(
            "subtitle_translation_repair",
            build_subtitle_repair_prompt(
                source_blocks=source_blocks,
                draft_translations=draft_translations,
                issues=issues,
                target_lang=target_lang,
                style=style,
                translation_plan=translation_plan,
            ),
        )

    def generate_bilibili_tags(self, *, title: str, summary: str, transcript: str, n_tags: int = 6) -> list[str]:
        data = self._request_json_prompt(
            "bilibili_tags",
            build_bilibili_tags_prompt(title=title, summary=summary, transcript=transcript, n_tags=n_tags),
        )
        return _clean_bilibili_tags(data.get("tags"), n_tags=max(1, int(n_tags)))

    def recommend_typeid(self, text: str, *, options: list[dict[str, Any]]) -> dict[str, Any]:
        source = str(text or "").strip()
        if not source:
            raise ValueError("text is empty")
        if not options:
            raise ValueError("options is empty")
        return self._request_json_prompt(
            "bilibili_typeid",
            build_typeid_prompt(source, options=options),
        )

    def review_publish_content(self, *, title: str, summary: str, subtitle_excerpt: str, reject_rules: str) -> dict[str, Any]:
        return self._request_json_prompt(
            "publish_review",
            build_publish_review_prompt(
                title=title,
                summary=summary,
                subtitle_excerpt=subtitle_excerpt,
                reject_rules=reject_rules,
            ),
        )


def _clean_bilibili_tags(raw_tags: Any, *, n_tags: int) -> list[str]:
    if not isinstance(raw_tags, list):
        raise RuntimeError("OpenAI output missing 'tags' list")

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_tags:
        s = str(item or "").strip().lstrip("#").lstrip("＃")
        s = "".join(s.split())
        if not s:
            continue
        if s.lower() == "videoroll":
            continue
        if len(s) > 20:
            s = s[:20]
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)

    if len(out) < n_tags:
        raise RuntimeError(f"OpenAI output has too few tags (want={n_tags}, got={len(out)})")
    return out[:n_tags]


def translate_text_openai(
    text: str,
    *,
    target_lang: str,
    style: str,
    config: OpenAIChatConfig,
) -> str:
    source = str(text or "").strip()
    if not source:
        return text
    prompt = build_text_translation_prompt(source, target_lang=target_lang, style=style)
    data = request_openai_json_object(
        config=config,
        system_prompt=prompt.system_prompt,
        user_prompt=prompt.user_prompt,
    )
    translated = str(data.get("translation") or "").strip()
    return translated or source


def translate_title_openai(
    title: str,
    *,
    target_lang: str,
    style: str,
    summary: str,
    config: OpenAIChatConfig,
    retry_count: int = 4,
    retry_delay_seconds: float = 1.0,
) -> str:
    source = str(title or "").strip()
    if not source:
        return title
    prompt = build_title_translation_prompt(
        source,
        target_lang=target_lang,
        style=style,
        summary=summary,
    )
    attempts = max(1, min(10, int(retry_count) + 1))
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            data = request_openai_json_object(
                config=config,
                system_prompt=prompt.system_prompt,
                user_prompt=prompt.user_prompt,
                format_retry_notice=prompt.format_retry_notice,
                format_retries=prompt.format_retries,
                network_retries=prompt.network_retries,
            )
            translated = str(data.get("translation") or "").strip()
            if not translated:
                raise RuntimeError("OpenAI title translation output is empty")
            return translated
        except Exception as error:
            last_error = error
            if attempt >= attempts - 1:
                break
            delay = max(0.0, float(retry_delay_seconds)) * min(8.0, float(2**attempt))
            if delay > 0:
                time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise RuntimeError("OpenAI title translation failed")


def generate_bilibili_tags_openai(
    *,
    title: str,
    summary: str,
    transcript: str,
    config: OpenAIChatConfig,
    n_tags: int = 6,
) -> list[str]:
    n = max(1, int(n_tags))
    prompt = build_bilibili_tags_prompt(title=title, summary=summary, transcript=transcript, n_tags=n)
    data = request_openai_json_object(
        config=config,
        system_prompt=prompt.system_prompt,
        user_prompt=prompt.user_prompt,
    )
    return _clean_bilibili_tags(data.get("tags"), n_tags=n)


def recommend_typeid_openai(
    text: str,
    *,
    options: list[dict[str, Any]],
    config: OpenAIChatConfig,
) -> dict[str, Any]:
    source = str(text or "").strip()
    if not source:
        raise ValueError("text is empty")
    if not options:
        raise ValueError("options is empty")
    prompt = build_typeid_prompt(source, options=options)
    return request_openai_json_object(
        config=config,
        system_prompt=prompt.system_prompt,
        user_prompt=prompt.user_prompt,
    )


def review_publish_content_openai(
    *,
    title: str,
    summary: str,
    subtitle_excerpt: str,
    reject_rules: str,
    config: OpenAIChatConfig,
) -> dict[str, Any]:
    prompt = build_publish_review_prompt(
        title=title,
        summary=summary,
        subtitle_excerpt=subtitle_excerpt,
        reject_rules=reject_rules,
    )
    return request_openai_json_object(
        config=config,
        system_prompt=prompt.system_prompt,
        user_prompt=prompt.user_prompt,
    )
