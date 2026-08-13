from __future__ import annotations

import unittest

from videoroll.apps.youtube_settings_store import YOUTUBE_SETTINGS_KEY, get_youtube_settings, update_youtube_settings
from videoroll.db.models import AppSetting


class _FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, AppSetting] = {}

    def get(self, model: object, key: str) -> AppSetting | None:
        assert model is AppSetting
        return self.rows.get(key)

    def add(self, row: AppSetting) -> None:
        self.rows[row.key] = row

    def commit(self) -> None:
        return None

    def refresh(self, row: AppSetting) -> None:
        self.rows[row.key] = row


class YouTubeSettingsStoreTests(unittest.TestCase):
    def test_youtube_compatibility_mode_is_disabled_by_default(self) -> None:
        self.assertFalse(get_youtube_settings(_FakeDb())["compatibility_mode_enabled"])

    def test_youtube_compatibility_mode_can_be_enabled_and_disabled(self) -> None:
        db = _FakeDb()

        enabled = update_youtube_settings(db, {"compatibility_mode_enabled": True})
        self.assertTrue(enabled["compatibility_mode_enabled"])
        self.assertTrue(db.rows[YOUTUBE_SETTINGS_KEY].value_json["compatibility_mode_enabled"])

        disabled = update_youtube_settings(db, {"compatibility_mode_enabled": False})
        self.assertFalse(disabled["compatibility_mode_enabled"])


if __name__ == "__main__":
    unittest.main()
