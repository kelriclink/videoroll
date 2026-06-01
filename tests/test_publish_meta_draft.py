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
