from __future__ import annotations

import json

import httpx

from videoroll.ai.client import OpenAIChatConfig, OpenAIToolCall, OpenAIToolTurn, parse_openai_tool_turn, request_openai_tool_turn
from videoroll.apps.subtitle_service.rag import rag_settings_from_translate_settings


def test_openai_tool_turn_uses_native_chat_protocol() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-search",
                                    "type": "function",
                                    "function": {"name": "search_web", "arguments": '{"query":"VGA red signal"}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        turn = request_openai_tool_turn(
            config=OpenAIChatConfig(api_key="test", base_url="https://llm.example/v1", model="demo"),
            messages=[{"role": "user", "content": "Research VGA"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "description": "Search the configured service.",
                        "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
            client=client,
            max_completion_tokens=123,
        )

    assert captured["tool_choice"] == "auto"
    assert captured["max_completion_tokens"] == 123
    assert captured["tools"][0]["function"]["name"] == "search_web"  # type: ignore[index]
    assert turn.tool_calls[0].id == "call-search"
    assert turn.tool_calls[0].arguments == {"query": "VGA red signal"}


def test_tool_argument_parse_error_is_returned_for_server_validation() -> None:
    turn = parse_openai_tool_turn(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "bad-call", "type": "function", "function": {"name": "search_web", "arguments": "not-json"}}
                        ],
                    },
                }
            ]
        }
    )

    assert turn.tool_calls[0].arguments == {}
    assert "invalid JSON" in turn.tool_calls[0].argument_error


def test_research_agent_preserves_assistant_and_tool_messages(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    requests: list[list[dict[str, object]]] = []
    turns = iter(
        [
            OpenAIToolTurn(
                content="",
                assistant_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "search-1",
                            "type": "function",
                            "function": {"name": "search_web", "arguments": '{"query":"VGA red signal"}'},
                        }
                    ],
                },
                tool_calls=[OpenAIToolCall(id="search-1", name="search_web", arguments={"query": "VGA red signal"})],
                finish_reason="tool_calls",
            ),
            OpenAIToolTurn(
                content="",
                assistant_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "fetch-1",
                            "type": "function",
                            "function": {"name": "fetch_url", "arguments": '{"url":"https://example.com/vga"}'},
                        }
                    ],
                },
                tool_calls=[OpenAIToolCall(id="fetch-1", name="fetch_url", arguments={"url": "https://example.com/vga"})],
                finish_reason="tool_calls",
            ),
            OpenAIToolTurn(
                content="Evidence is sufficient.",
                assistant_message={"role": "assistant", "content": "Evidence is sufficient."},
                tool_calls=[],
                finish_reason="stop",
            ),
        ]
    )

    def fake_turn(**kwargs):
        requests.append([dict(message) for message in kwargs["messages"]])
        return next(turns)

    monkeypatch.setattr(rag_module, "request_openai_tool_turn", fake_turn)
    monkeypatch.setattr(rag_module, "_append_agent_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "_append_llm_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rag_module,
        "fetch_search_evidence",
        lambda *_args, **_kwargs: [
            {"title": "VGA signal guide", "url": "https://example.com/vga", "snippet": "A red VGA signal indicates...", "tool": "search"}
        ],
    )
    monkeypatch.setattr(
        rag_module,
        "fetch_url_evidence",
        lambda **_kwargs: {
            "title": "VGA signal guide",
            "url": "https://example.com/vga",
            "snippet": "A red VGA signal indicates...",
            "content": "A red VGA signal indicates a connection fault in this device.",
            "tool": "fetch",
        },
    )

    evidence, tools_used, rounds = rag_module._collect_evidence_with_tool_agent(
        object(),  # type: ignore[arg-type]
        agent_run_id=None,
        term="VGA",
        domain_hint="hardware",
        target_lang="zh",
        rag_settings=rag_settings_from_translate_settings(
            {"rag_enabled": True, "rag_search_enabled": True, "rag_search_url": "https://search.example/search"}
        ),
        chat_config=OpenAIChatConfig(api_key="test", base_url="https://llm.example/v1", model="demo"),
        llm_context="The VGA indicator is red.",
        search_queries=["VGA red signal"],
        max_steps=4,
    )

    assert tools_used == ["search_web", "fetch_url"]
    assert rounds == 3
    assert evidence[0]["url"] == "https://example.com/vga"
    assert evidence[0]["content"] == "A red VGA signal indicates a connection fault in this device."
    assert requests[1][-2]["role"] == "assistant"
    assert requests[1][-1]["role"] == "tool"
    assert requests[1][-1]["tool_call_id"] == "search-1"
