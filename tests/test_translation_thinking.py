from __future__ import annotations

import json

import httpx

from videoroll.ai.client import (
    OpenAIChatConfig,
    openai_chat_config_from_settings,
    request_openai_json_object,
    request_openai_json_object_with_thinking,
)
from videoroll.apps.subtitle_service.processing import Segment, translate_segments_openai_with_summary


def test_openai_thinking_streams_reasoning_and_collects_json() -> None:
    seen_request: dict[str, object] = {}
    thought: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content.decode("utf-8")))
        events = "\n\n".join(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"先理解"}}]}',
                'data: {"choices":[{"delta":{"reasoning_details":[{"text":"再翻译"}]}}]}',
                'data: {"choices":[{"delta":{"content":"{\\"translation\\":\\"你好\\"}"}}]}',
                "data: [DONE]",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events.encode("utf-8"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_openai_json_object_with_thinking(
            config=OpenAIChatConfig(api_key="test", base_url="https://llm.example/v1", model="think-model"),
            system_prompt="json only",
            user_prompt="translate",
            on_thinking_delta=thought.append,
            client=client,
        )

    assert result == {"translation": "你好"}
    assert thought == ["先理解", "再翻译"]
    assert seen_request["stream"] is True
    assert seen_request["enable_thinking"] is True
    assert seen_request["response_format"] == {"type": "json_object"}


def test_cerebras_thinking_uses_reasoning_protocol_and_streams_reasoning() -> None:
    seen_request: dict[str, object] = {}
    thought: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content.decode("utf-8")))
        events = "\n\n".join(
            [
                'data: {"choices":[{"delta":{"reasoning":"先分析术语"}}]}',
                'data: {"choices":[{"delta":{"content":"{\\"translation\\":\\"你好\\"}"}}]}',
                "data: [DONE]",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events.encode("utf-8"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_openai_json_object_with_thinking(
            config=OpenAIChatConfig(
                api_key="test",
                base_url="https://llm.example/v1",
                model="gpt-oss-120b",
                api_type="cerebras",
                cerebras_reasoning_effort="high",
                cerebras_reasoning_format="parsed",
            ),
            system_prompt="json only",
            user_prompt="translate",
            on_thinking_delta=thought.append,
            client=client,
        )

    assert result == {"translation": "你好"}
    assert thought == ["先分析术语"]
    assert seen_request["stream"] is True
    assert seen_request["reasoning_effort"] == "high"
    assert seen_request["reasoning_format"] == "parsed"
    assert "enable_thinking" not in seen_request
    assert "response_format" not in seen_request


def test_normal_json_request_does_not_opt_into_thinking() -> None:
    seen_request: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_openai_json_object(
            config=OpenAIChatConfig(api_key="test", base_url="https://llm.example/v1", model="plain-model"),
            system_prompt="json only",
            user_prompt="plain",
            client=client,
        )

    assert result == {"ok": True}
    assert "stream" not in seen_request
    assert "enable_thinking" not in seen_request


def test_openai_config_reads_and_normalizes_cerebras_settings() -> None:
    config = openai_chat_config_from_settings(
        {
            "openai_api_key": "test",
            "openai_base_url": "https://llm.example/v1",
            "openai_model": "gpt-oss-120b",
            "openai_api_type": "cerebras",
            "cerebras_reasoning_effort": "HIGH",
            "cerebras_reasoning_format": "parsed",
        }
    )

    assert config.api_type == "cerebras"
    assert config.cerebras_reasoning_effort == "high"
    assert config.cerebras_reasoning_format == "parsed"


def test_subtitle_thinking_callback_receives_batch_coordinates() -> None:
    deltas: list[tuple[str, int, int]] = []

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["enable_thinking"] is True
            callback = kwargs["on_thinking_delta"]
            assert callable(callback)
            callback("checking terminology")
            return {"updated_summary": "done", "translations": [{"idx": 1, "text": "你好"}]}

    translated, summary = translate_segments_openai_with_summary(
        [Segment(start=0.0, end=1.0, text="hello")],
        target_lang="zh",
        style="自然",
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        enable_thinking=True,
        on_thinking_delta=lambda delta, start, size: deltas.append((delta, start, size)),
    )

    assert [segment.text for segment in translated] == ["你好"]
    assert summary == "done"
    assert deltas == [("checking terminology", 1, 1)]


def test_subtitle_batch_context_is_shared_by_rag_thinking_and_completion() -> None:
    events: list[tuple[str, object]] = []

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            callback = kwargs["on_thinking_delta"]
            assert callable(callback)
            callback("reasoning")
            return {
                "updated_summary": "done",
                "translations": [
                    {"idx": 1, "text": "一"},
                    {"idx": 2, "text": "二"},
                ],
            }

    def on_start(batch: list[Segment], start_idx: int, summary: str) -> dict[str, object]:
        context = {"batch_id": "batch-1"}
        events.append(("start", (len(batch), start_idx, summary, context)))
        return context

    translated, summary = translate_segments_openai_with_summary(
        [
            Segment(start=0.0, end=1.0, text="one"),
            Segment(start=1.0, end=2.0, text="two"),
        ],
        target_lang="zh",
        style="自然",
        batch_size=2,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        enable_thinking=True,
        on_batch_start=on_start,
        rag_context_provider_with_context=lambda _batch, _start, _summary, context: (
            events.append(("rag", context)) or {"term_cards": []}
        ),
        on_thinking_delta_with_context=lambda delta, start, size, context: events.append(
            ("thinking", (delta, start, size, context))
        ),
        on_batch_done_with_context=lambda context, batch, updated_summary, completed: events.append(
            ("done", (context, [segment.text for segment in batch], updated_summary, completed))
        ),
    )

    assert [segment.text for segment in translated] == ["一", "二"]
    assert summary == "done"
    context = events[0][1][3]  # type: ignore[index]
    assert events == [
        ("start", (2, 0, "", context)),
        ("rag", context),
        ("thinking", ("reasoning", 1, 2, context)),
        ("done", (context, ["一", "二"], "done", 2)),
    ]


def test_subtitle_batch_error_context_is_reported_before_timeout_halving() -> None:
    starts: list[dict[str, int]] = []
    errors: list[tuple[dict[str, int], str]] = []
    completed: list[tuple[dict[str, int], int]] = []
    calls = 0

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            blocks = kwargs["blocks"]
            assert isinstance(blocks, list)
            if calls == 1:
                raise httpx.ReadTimeout("batch too large")
            return {
                "updated_summary": f"done-{calls}",
                "translations": [{"idx": block["idx"], "text": f"译{block['idx']}"} for block in blocks],
            }

    def on_start(batch: list[Segment], start_idx: int, _summary: str) -> dict[str, int]:
        context = {"start": start_idx, "size": len(batch)}
        starts.append(context)
        return context

    translated, _summary = translate_segments_openai_with_summary(
        [
            Segment(start=0.0, end=1.0, text="one"),
            Segment(start=1.0, end=2.0, text="two"),
        ],
        target_lang="zh",
        style="自然",
        batch_size=2,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        on_batch_start=on_start,
        on_batch_error=lambda context, error: errors.append((context, str(error))),
        on_batch_done_with_context=lambda context, _batch, _summary, count: completed.append((context, count)),
    )

    assert [segment.text for segment in translated] == ["译1", "译2"]
    assert starts == [{"start": 0, "size": 2}, {"start": 0, "size": 1}, {"start": 1, "size": 1}]
    assert errors == [({"start": 0, "size": 2}, "batch too large")]
    assert completed == [({"start": 0, "size": 1}, 1), ({"start": 1, "size": 1}, 2)]
