from __future__ import annotations

import types
import unittest

from videoroll.apps.subtitle_service.auto_profile_store import AUTO_PROFILE_KEY, get_auto_profile


class _FakeDb:
    def __init__(self, value_json: dict[str, object] | None = None) -> None:
        self._row = types.SimpleNamespace(value_json=value_json) if value_json is not None else None

    def get(self, _model: object, key: str) -> object | None:
        assert key == AUTO_PROFILE_KEY
        return self._row


class AutoProfileStoreTests(unittest.TestCase):
    def test_get_auto_profile_uses_current_font_scale_defaults(self) -> None:
        profile = get_auto_profile(_FakeDb())

        self.assertEqual(profile["primary_font_scale_percent"], 100)
        self.assertEqual(profile["secondary_font_scale_percent"], 100)
        self.assertEqual(profile["auto_publish_platforms"], [])

    def test_get_auto_profile_normalizes_auto_publish_platforms(self) -> None:
        profile = get_auto_profile(
            _FakeDb({"auto_publish_platforms": ["douyin", "invalid", "bilibili", "douyin"]})
        )

        self.assertEqual(profile["auto_publish_platforms"], ["douyin", "bilibili"])

    def test_get_auto_profile_clamps_and_normalizes_font_scale_percent(self) -> None:
        profile = get_auto_profile(
            _FakeDb(
                {
                    "primary_font_scale_percent": "180",
                    "secondary_font_scale_percent": 999,
                }
            )
        )

        self.assertEqual(profile["primary_font_scale_percent"], 180)
        self.assertEqual(profile["secondary_font_scale_percent"], 300)

    def test_get_auto_profile_accepts_cloudflare_workers_ai(self) -> None:
        profile = get_auto_profile(_FakeDb({"asr_engine": "cloudflare-workers-ai"}))

        self.assertEqual(profile["asr_engine"], "cloudflare-workers-ai")


if __name__ == "__main__":
    unittest.main()


def test_auto_profile_translation_selection_validation_only_runs_for_selection_changes(
    monkeypatch,
) -> None:
    from videoroll.apps.subtitle_service import main as subtitle_main

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        subtitle_main,
        "get_translate_settings",
        lambda *_args: {"default_provider": "openai"},
    )
    monkeypatch.setattr(
        subtitle_main,
        "resolve_profile_translation_runtime",
        lambda _db, _legacy, profile: calls.append(dict(profile)),
    )
    current = {
        "translate_enabled": True,
        "translate_provider": "openai",
        "translate_model_provider_id": "legacy",
        "translate_model_id": "",
    }

    subtitle_main._validate_auto_profile_translation_selection(
        object(), object(), current, {"video_codec": "h264"}
    )
    assert calls == []

    subtitle_main._validate_auto_profile_translation_selection(
        object(),
        object(),
        current,
        {"translate_model_provider_id": "provider-x", "translate_model_id": "model-x"},
    )
    assert calls[-1]["translate_model_provider_id"] == "provider-x"
    assert calls[-1]["translate_model_id"] == "model-x"


def test_auto_profile_translation_selection_validation_skips_disabled_or_index(
    monkeypatch,
) -> None:
    from videoroll.apps.subtitle_service import main as subtitle_main

    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("resolver should not run")

    monkeypatch.setattr(subtitle_main, "resolve_profile_translation_runtime", fail_if_called)
    current = {
        "translate_enabled": True,
        "translate_provider": "openai",
        "translate_model_provider_id": "legacy",
        "translate_model_id": "",
    }

    subtitle_main._validate_auto_profile_translation_selection(
        object(), object(), current, {"translate_enabled": False}
    )
    subtitle_main._validate_auto_profile_translation_selection(
        object(), object(), current, {"translate_provider": "index_translate"}
    )
    assert called is False
