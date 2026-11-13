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


def test_default_tag_template_is_empty():
    db = _Session()
    assert get_bilibili_publish_settings(db)["default_meta"].tags == []


def test_single_historical_videoroll_default_is_replaced_in_memory():
    from types import SimpleNamespace
    db = _Session()
    db.row = SimpleNamespace(value_json={"default_meta": {
        "title": "旧模板", "typeid": 17, "tags": ["videoroll"]
    }})
    settings = get_bilibili_publish_settings(db)
    assert settings["default_meta"].tags == []
    assert db.row.value_json["default_meta"]["tags"] == ["videoroll"]


def test_historical_video_share_default_is_empty_without_database_mutation():
    from types import SimpleNamespace

    db = _Session()
    db.row = SimpleNamespace(value_json={"default_meta": {
        "title": "旧模板", "typeid": 17, "tags": ["视频分享"]
    }})
    settings = get_bilibili_publish_settings(db)
    assert settings["default_meta"].tags == []
    assert db.row.value_json["default_meta"]["tags"] == ["视频分享"]


def test_user_can_save_empty_default_meta_but_publish_requires_tags():
    import pytest

    from videoroll.apps.bilibili_publisher.schemas import (
        BilibiliPublishMeta, BilibiliPublishSettingsRead,
    )
    from pydantic import ValidationError

    db = _Session()
    settings = update_bilibili_publish_settings(db, {
        "default_meta": {"title": "模板", "typeid": 17, "tags": []}
    })
    assert settings["default_meta"].tags == []
    assert db.row.value_json["default_meta"]["tags"] == []
    assert BilibiliPublishSettingsRead(**settings).default_meta["tags"] == []
    with pytest.raises(ValidationError, match="meta.tags is required"):
        BilibiliPublishMeta.model_validate({
            "title": "即将投稿", "typeid": 17, "tags": []
        })


def test_user_configured_additional_tags_are_preserved():
    from types import SimpleNamespace
    db = _Session()
    db.row = SimpleNamespace(value_json={"default_meta": {
        "title": "自定义", "typeid": 17, "tags": ["videoroll", "硬件"]
    }})
    assert get_bilibili_publish_settings(db)["default_meta"].tags == ["videoroll", "硬件"]


def test_custom_default_tags_remain_supported():
    db = _Session()
    settings = update_bilibili_publish_settings(db, {
        "default_meta": {"title": "自定义", "typeid": 17, "tags": ["科技"]}
    })
    assert settings["default_meta"].tags == ["科技"]


def test_no_forced_videoroll_in_publishing_tag_merge():
    from videoroll.apps.publish_gateway import merge_bilibili_tags
    assert merge_bilibili_tags(["USB-C", "科技"], ["USB-C", "快充"]) == ["USB-C", "科技", "快充"]
    assert merge_bilibili_tags([], ["videoroll"]) == ["videoroll"]
    assert merge_bilibili_tags(["USB-C"], []) == ["USB-C"]
    assert merge_bilibili_tags(["tag"] * 20, ["title"]) == ["tag", "title"]
