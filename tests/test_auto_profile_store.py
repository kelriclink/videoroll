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


if __name__ == "__main__":
    unittest.main()
