import uuid
from types import SimpleNamespace

import pytest

from videoroll.apps.bilibili_publisher.constants import BILIBILI_DESC_MAX_CHARS
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta
from videoroll.apps.publish_meta_rules import (
    append_title_uploader_suffix,
    apply_publish_source_overrides,
    bilibili_text_units,
    build_bilibili_desc,
)


def test_build_bilibili_desc_prepends_source_once() -> None:
    desc = build_bilibili_desc(
        "原视频：https://youtube.com/watch?v=abc\n\nsecond line",
        "https://youtube.com/watch?v=abc",
    )
    assert desc == "原视频：https://youtube.com/watch?v=abc\n\nsecond line"


def test_build_bilibili_desc_puts_uploader_on_next_line() -> None:
    desc = build_bilibili_desc(
        "second line",
        "https://youtube.com/watch?v=abc",
        "Usagi Electric",
    )
    assert desc == "原视频：https://youtube.com/watch?v=abc\n博主：Usagi Electric\n\nsecond line"


def test_build_bilibili_desc_clamps_body_and_keeps_source_block() -> None:
    desc = build_bilibili_desc(
        "x" * 5000,
        "https://youtube.com/watch?v=abc",
        "Usagi Electric",
    )
    assert len(desc) <= BILIBILI_DESC_MAX_CHARS
    assert desc.startswith("原视频：https://youtube.com/watch?v=abc\n博主：Usagi Electric\n\n")


def test_build_bilibili_desc_clamps_bilibili_wide_units() -> None:
    source_url = "https://www.youtube.com/watch?v=sn1Y6zIS91g"
    source_uploader = "AlphaPhoenix"
    source_block = f"原视频：{source_url}\n博主：{source_uploader}"
    base_len = BILIBILI_DESC_MAX_CHARS - len(source_block) - len("\n\n") - 1
    original = f"{source_block}\n\n{'a' * base_len}"

    assert len(original) < BILIBILI_DESC_MAX_CHARS
    assert bilibili_text_units(original) > BILIBILI_DESC_MAX_CHARS

    desc = build_bilibili_desc("a" * base_len, source_url, source_uploader)

    assert bilibili_text_units(desc) <= BILIBILI_DESC_MAX_CHARS
    assert desc.startswith(source_block)
    assert desc.endswith("…")


def test_publish_meta_model_clamps_desc_by_bilibili_wide_units() -> None:
    source_url = "https://www.youtube.com/watch?v=sn1Y6zIS91g"
    source_block = f"原视频：{source_url}\n博主：AlphaPhoenix"
    base_len = BILIBILI_DESC_MAX_CHARS - len(source_block) - len("\n\n") - 1
    original = f"{source_block}\n\n{'a' * base_len}"

    meta = BilibiliPublishMeta.model_validate(
        {
            "title": "test",
            "desc": original,
            "typeid": 17,
            "tags": ["videoroll"],
        }
    )

    assert bilibili_text_units(meta.desc) <= BILIBILI_DESC_MAX_CHARS
    assert meta.desc.endswith("…")


def test_build_bilibili_desc_replaces_existing_source_prefix_block() -> None:
    desc = build_bilibili_desc(
        "原视频：https://youtube.com/watch?v=abc\n博主：Old Name\n\nsecond line",
        "https://youtube.com/watch?v=abc",
        "New Name",
    )
    assert desc == "原视频：https://youtube.com/watch?v=abc\n博主：New Name\n\nsecond line"


def test_apply_publish_source_overrides_uses_backend_rules() -> None:
    meta = apply_publish_source_overrides(
        {
            "title": "示例标题",
            "desc": "示例简介",
            "tags": ["videoroll"],
            "typeid": 17,
            "copyright": 1,
            "source": "",
        },
        source_title="Original title",
        translated_title="中文标题",
        source_description="youtube desc",
        source_url="https://youtube.com/watch?v=abc",
        source_uploader="Usagi Electric",
        title_prefix="【熟肉】",
        enable_reprint=True,
    )

    assert meta["title"] == "【熟肉】中文标题-Usagi Electric"
    assert meta["desc"] == "原视频：https://youtube.com/watch?v=abc\n博主：Usagi Electric\n\nyoutube desc"
    assert meta["copyright"] == 2
    assert meta["source"] == "https://youtube.com/watch?v=abc"


def test_append_title_uploader_suffix_avoids_duplicate_suffix() -> None:
    assert append_title_uploader_suffix("【熟肉】中文标题-Usagi Electric", "Usagi Electric") == "【熟肉】中文标题-Usagi Electric"


def test_append_title_uploader_suffix_preserves_suffix_when_clamping() -> None:
    title = append_title_uploader_suffix("x" * 100, "Usagi Electric")
    assert title.endswith("-Usagi Electric")
    assert len(title) <= 80


def test_publish_meta_draft_resolves_selected_model_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.apps import publish_meta_draft as module

    task = SimpleNamespace(
        id=uuid.uuid4(),
        source_type=SimpleNamespace(value="youtube"),
        source_url="https://youtube.com/watch?v=demo",
    )
    profile = {
        "translate_provider": "openai",
        "translate_model_provider_id": "provider-x",
        "translate_model_id": "model-x",
        "publish_translate_title": True,
    }
    legacy = {
        "default_provider": "openai",
        "openai_model": "legacy-model",
        "openai_api_key": "legacy-key",
    }
    runtime_settings = {
        **legacy,
        "openai_model": "model-x",
        "openai_api_key": "custom-key",
        "openai_base_url": "https://custom.example/v1",
    }
    captured: dict[str, object] = {}

    monkeypatch.setattr(module, "default_publish_meta", lambda _db: {"title": ""})
    monkeypatch.setattr(module, "get_task_titles", lambda *_args: {})
    monkeypatch.setattr(
        module,
        "_read_latest_youtube_meta",
        lambda *_args, **_kwargs: {
            "title": "Original",
            "description": "",
            "webpage_url": task.source_url,
            "uploader": "",
        },
    )
    monkeypatch.setattr(module, "get_task_bilibili_summary", lambda *_args: "")
    monkeypatch.setattr(module, "get_auto_profile", lambda _db: profile)
    monkeypatch.setattr(module, "get_translate_settings", lambda *_args: legacy)
    monkeypatch.setattr(module, "get_subtitle_settings", lambda: SimpleNamespace())

    def resolve_runtime(_db, _legacy, **selection):
        captured["selection"] = selection
        return SimpleNamespace(settings=runtime_settings)

    class FakeAIService:
        def __init__(self, settings_factory):
            captured["service_settings"] = settings_factory()

    def apply_overrides(meta, **kwargs):
        captured["translate_settings"] = kwargs["translate_settings"]
        captured["ai_service"] = kwargs["ai_service"]
        return meta

    monkeypatch.setattr(module, "resolve_translation_runtime", resolve_runtime)
    monkeypatch.setattr(module, "AIService", FakeAIService)
    monkeypatch.setattr(module, "apply_publish_source_overrides", apply_overrides)

    result = module.build_task_publish_meta_draft(
        task,
        db=SimpleNamespace(),
        store=SimpleNamespace(),
    )

    assert result == {"title": ""}
    assert captured["selection"] == {
        "provider": "openai",
        "model_provider_id": "provider-x",
        "model_id": "model-x",
    }
    assert captured["translate_settings"] is runtime_settings
    assert captured["service_settings"] == runtime_settings


def test_enabled_title_translation_fails_closed() -> None:
    from videoroll.apps.publish_meta_draft import (
        PublishTitleTranslationError,
        translate_publish_title,
    )

    class BrokenAIService:
        def translate_title(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    with pytest.raises(PublishTitleTranslationError, match="provider unavailable"):
        translate_publish_title(
            "Original title",
            profile={
                "publish_translate_title": True,
                "translate_provider": "openai",
                "target_lang": "zh",
            },
            translate_settings={
                "openai_api_key": "configured",
                "default_target_lang": "zh",
                "default_style": "口语自然",
            },
            ai_service=BrokenAIService(),
        )
