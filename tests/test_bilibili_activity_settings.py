from __future__ import annotations

from videoroll.apps.bilibili_publisher.publish_settings_store import (
    get_bilibili_publish_settings,
    update_bilibili_publish_settings,
)


class _Session:
    row = None

    def get(self, _model, _key):
        return self.row

    def add(self, row):
        self.row = row

    def commit(self):
        pass

    def refresh(self, _row):
        pass


def test_activity_toggle_is_default_off_and_persists():
    db = _Session()
    original = get_bilibili_publish_settings(db)
    assert original["ai_activity_enabled"] is False
    original_tags = original["default_meta"].tags

    enabled = update_bilibili_publish_settings(db, {"ai_activity_enabled": True})
    assert enabled["ai_activity_enabled"] is True
    assert enabled["default_meta"].tags == original_tags

    disabled = update_bilibili_publish_settings(db, {"ai_activity_enabled": False})
    assert disabled["ai_activity_enabled"] is False


def test_activity_toggle_rejects_non_boolean():
    db = _Session()
    try:
        update_bilibili_publish_settings(db, {"ai_activity_enabled": "true"})
    except ValueError as error:
        assert "boolean" in str(error)
    else:
        raise AssertionError("non-boolean toggle value was accepted")
