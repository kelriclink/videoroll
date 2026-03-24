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

from videoroll.ai.client import OpenAIChatConfig, openai_chat_config_from_settings
from videoroll.ai import service


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


class AIServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = OpenAIChatConfig(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="fake-model",
            temperature=0.2,
            timeout_seconds=30.0,
        )

    def test_openai_chat_config_from_settings(self) -> None:
        cfg = openai_chat_config_from_settings(
            {
                "openai_api_key": "abc",
                "openai_base_url": "https://api.example.com/v1",
                "openai_model": "gpt-test",
                "openai_temperature": 0.7,
                "openai_timeout_seconds": 12,
            }
        )
        self.assertEqual(cfg.api_key, "abc")
        self.assertEqual(cfg.base_url, "https://api.example.com/v1")
        self.assertEqual(cfg.model, "gpt-test")
        self.assertEqual(cfg.temperature, 0.7)
        self.assertEqual(cfg.timeout_seconds, 12.0)

    def test_translate_text_openai(self) -> None:
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"translation": "你好世界"}, ensure_ascii=False)
                            }
                        }
                    ]
                }
            )
        ]
        seen_requests: list[dict[str, object]] = []

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

        with patch("videoroll.ai.client.httpx.Client", FakeClient):
            translated = service.translate_text_openai(
                "Hello world",
                target_lang="zh",
                style="自然",
                config=self.config,
            )

        self.assertEqual(translated, "你好世界")
        self.assertIn('"translation"', str(seen_requests[0]["messages"][1]["content"]))

    def test_generate_bilibili_tags_openai_cleans_tags(self) -> None:
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "tags": [
                                            " AI ",
                                            "#机器学习",
                                            "videoroll",
                                            "机器学习",
                                            "深度 学习",
                                            "特别特别特别特别特别长的标签名字",
                                        ]
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
                del headers, json
                return responses.pop(0)

        with patch("videoroll.ai.client.httpx.Client", FakeClient):
            tags = service.generate_bilibili_tags_openai(
                title="AI title",
                summary="summary",
                transcript="transcript",
                config=self.config,
                n_tags=4,
            )

        self.assertEqual(tags, ["AI", "机器学习", "深度学习", "特别特别特别特别特别长的标签名字"[:20]])

    def test_recommend_typeid_openai(self) -> None:
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"typeid": 17, "reason": "最匹配科技内容"}, ensure_ascii=False)
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
                del headers, json
                return responses.pop(0)

        with patch("videoroll.ai.client.httpx.Client", FakeClient):
            obj = service.recommend_typeid_openai(
                "AI 芯片评测",
                options=[{"id": 17, "path": "科技/数码"}],
                config=self.config,
            )

        self.assertEqual(obj["typeid"], 17)
        self.assertEqual(obj["reason"], "最匹配科技内容")

    def test_review_publish_content_openai(self) -> None:
        responses = [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"approved": False, "reason": "包含危险内容", "risk_tags": ["危险", "教程"]},
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )
        ]
        seen_requests: list[dict[str, object]] = []

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

        with patch("videoroll.ai.client.httpx.Client", FakeClient):
            obj = service.review_publish_content_openai(
                title="标题",
                summary="总结",
                subtitle_excerpt="字幕片段",
                reject_rules="危险教程一律不通过",
                config=self.config,
            )

        self.assertEqual(obj["approved"], False)
        self.assertEqual(obj["reason"], "包含危险内容")
        self.assertIn("危险教程一律不通过", str(seen_requests[0]["messages"][1]["content"]))


if __name__ == "__main__":
    unittest.main()
