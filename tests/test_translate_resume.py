from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import patch

try:
    import httpx as _httpx  # type: ignore
except ModuleNotFoundError:
    fake_httpx = types.ModuleType("httpx")

    class TimeoutException(Exception):
        pass

    class TransportError(Exception):
        pass

    class HTTPStatusError(Exception):
        pass

    class Timeout:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class Response:
        pass

    fake_httpx.TimeoutException = TimeoutException
    fake_httpx.TransportError = TransportError
    fake_httpx.HTTPStatusError = HTTPStatusError
    fake_httpx.Timeout = Timeout
    fake_httpx.Client = Client
    fake_httpx.Response = Response
    sys.modules["httpx"] = fake_httpx

from videoroll.apps.subtitle_service import processing
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.ai.service import AIService


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class TranslateResumeTests(unittest.TestCase):
    def test_resume_uses_existing_prefix_and_summary(self) -> None:
        source_segments = [
            Segment(start=0.0, end=1.0, text="one"),
            Segment(start=1.0, end=2.0, text="two"),
            Segment(start=2.0, end=3.0, text="three"),
        ]
        resumed_segments = [Segment(start=0.0, end=1.0, text="一")]

        seen_requests: list[dict[str, object]] = []
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "updated_summary": "summary-2",
                                        "translations": [{"idx": 2, "text": "二"}],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "updated_summary": "summary-3",
                                        "translations": [{"idx": 3, "text": "三"}],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ),
        ]

        class FakeClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                return None

            def post(self, _url: str, *, headers: dict[str, str], json: dict[str, object]) -> _FakeResponse:
                del headers
                seen_requests.append(json)
                if not responses:
                    raise AssertionError("unexpected extra OpenAI request")
                return responses.pop(0)

        batch_events: list[tuple[list[str], str, int]] = []

        def on_batch_done(batch: list[Segment], summary: str, completed_count: int) -> None:
            batch_events.append(([seg.text for seg in batch], summary, completed_count))

        with patch.object(processing, "create_openai_http_client", lambda _timeout: FakeClient()):
            translated, summary = processing.translate_segments_openai_with_summary(
                source_segments,
                target_lang="zh",
                style="自然",
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="fake-model",
                batch_size=1,
                resume_from=resumed_segments,
                initial_summary="summary-1",
                on_batch_done=on_batch_done,
            )

        self.assertEqual([seg.text for seg in translated], ["一", "二", "三"])
        self.assertEqual(summary, "summary-3")
        self.assertEqual(batch_events, [(["二"], "summary-2", 2), (["三"], "summary-3", 3)])
        self.assertEqual(len(seen_requests), 2)

        first_prompt = seen_requests[0]["messages"][1]["content"]
        second_prompt = seen_requests[1]["messages"][1]["content"]
        self.assertIn('"summary": "summary-1"', str(first_prompt))
        self.assertIn('"idx": 2', str(first_prompt))
        self.assertIn('"summary": "summary-2"', str(second_prompt))
        self.assertIn('"idx": 3', str(second_prompt))

    def test_resume_prefix_cannot_be_longer_than_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "longer than the source segments"):
            processing.translate_segments_openai_with_summary(
                [Segment(start=0.0, end=1.0, text="one")],
                target_lang="zh",
                style="自然",
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="fake-model",
                resume_from=[
                    Segment(start=0.0, end=1.0, text="一"),
                    Segment(start=1.0, end=2.0, text="二"),
                ],
            )

    def test_missing_translations_resume_from_failed_position(self) -> None:
        source_segments = [
            Segment(start=0.0, end=1.0, text="one"),
            Segment(start=1.0, end=2.0, text="two"),
            Segment(start=2.0, end=3.0, text="three"),
        ]

        seen_requests: list[dict[str, object]] = []
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "updated_summary": "ignored-partial-summary",
                                        "translations": [
                                            {"idx": 1, "text": "一"},
                                            {"idx": 2, "text": "二"},
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "updated_summary": "summary-final",
                                        "translations": [{"idx": 3, "text": "三"}],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ),
        ]

        class FakeClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                return None

            def post(self, _url: str, *, headers: dict[str, str], json: dict[str, object]) -> _FakeResponse:
                del headers
                seen_requests.append(json)
                if not responses:
                    raise AssertionError("unexpected extra OpenAI request")
                return responses.pop(0)

        batch_events: list[tuple[list[str], str, int]] = []

        def on_batch_done(batch: list[Segment], summary: str, completed_count: int) -> None:
            batch_events.append(([seg.text for seg in batch], summary, completed_count))

        with patch.object(processing, "create_openai_http_client", lambda _timeout: FakeClient()):
            translated, summary = processing.translate_segments_openai_with_summary(
                source_segments,
                target_lang="zh",
                style="自然",
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="fake-model",
                batch_size=3,
                initial_summary="summary-start",
                on_batch_done=on_batch_done,
            )

        self.assertEqual([seg.text for seg in translated], ["一", "二", "三"])
        self.assertEqual(summary, "summary-final")
        self.assertEqual(batch_events, [(["一", "二"], "summary-start", 2), (["三"], "summary-final", 3)])
        self.assertEqual(len(seen_requests), 2)

        first_prompt = seen_requests[0]["messages"][1]["content"]
        second_prompt = seen_requests[1]["messages"][1]["content"]
        self.assertIn('"idx": 1', str(first_prompt))
        self.assertIn('"idx": 2', str(first_prompt))
        self.assertIn('"idx": 3', str(first_prompt))
        self.assertIn('"summary": "summary-start"', str(second_prompt))
        self.assertIn('"idx": 3', str(second_prompt))

    def test_rag_context_provider_is_included_in_prompt(self) -> None:
        source_segments = [Segment(start=0.0, end=1.0, text="Rush B with an AWP")]
        seen_requests: list[dict[str, object]] = []
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "updated_summary": "cs2",
                                        "translations": [{"idx": 1, "text": "快冲 B 点，带着 AWP 狙击枪"}],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )
        ]

        class FakeClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                return None

            def post(self, _url: str, *, headers: dict[str, str], json: dict[str, object]) -> _FakeResponse:
                del headers
                seen_requests.append(json)
                return responses.pop(0)

        def rag_context_provider(batch: list[Segment], start_idx: int, summary: str) -> dict[str, object]:
            self.assertEqual([seg.text for seg in batch], ["Rush B with an AWP"])
            self.assertEqual(start_idx, 0)
            self.assertEqual(summary, "")
            return {
                "term_cards": [
                    {
                        "term": "AWP",
                        "translation": "AWP 狙击枪",
                        "domain": "CS2",
                        "description": "Counter-Strike 系列中的狙击枪。",
                    }
                ]
            }

        with patch.object(processing, "create_openai_http_client", lambda _timeout: FakeClient()):
            translated, summary = processing.translate_segments_openai_with_summary(
                source_segments,
                target_lang="zh",
                style="自然",
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="fake-model",
                batch_size=1,
                rag_context_provider=rag_context_provider,
            )

        self.assertEqual([seg.text for seg in translated], ["快冲 B 点，带着 AWP 狙击枪"])
        self.assertEqual(summary, "cs2")
        prompt = str(seen_requests[0]["messages"][1]["content"])
        self.assertIn("rag_context", prompt)
        self.assertIn("AWP 狙击枪", prompt)

    def test_ai_service_resolves_model_for_each_batch(self) -> None:
        source_segments = [
            Segment(start=0.0, end=1.0, text="one"),
            Segment(start=1.0, end=2.0, text="two"),
        ]
        seen_requests: list[dict[str, object]] = []
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "updated_summary": "summary-1",
                                        "translations": [{"idx": 1, "text": "一"}],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "updated_summary": "summary-2",
                                        "translations": [{"idx": 2, "text": "二"}],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ),
        ]

        class FakeClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                return None

            def post(self, _url: str, *, headers: dict[str, str], json: dict[str, object]) -> _FakeResponse:
                del headers
                seen_requests.append(json)
                if not responses:
                    raise AssertionError("unexpected extra OpenAI request")
                return responses.pop(0)

        settings_calls = 0

        def settings_provider() -> dict[str, object]:
            nonlocal settings_calls
            settings_calls += 1
            return {
                "openai_api_key": "test-key",
                "openai_base_url": "https://example.invalid/v1",
                "openai_model": "old-model" if settings_calls == 1 else "new-model",
                "openai_temperature": 0.2,
                "openai_timeout_seconds": 30.0,
                "openai_max_retries": 3,
            }

        with patch("videoroll.ai.client.httpx.Client", FakeClient):
            translated, summary = processing.translate_segments_openai_with_summary(
                source_segments,
                target_lang="zh",
                style="自然",
                batch_size=1,
                ai_service=AIService(settings_provider),
            )

        self.assertEqual([seg.text for seg in translated], ["一", "二"])
        self.assertEqual(summary, "summary-2")
        self.assertEqual([req["model"] for req in seen_requests], ["old-model", "new-model"])


if __name__ == "__main__":
    unittest.main()
