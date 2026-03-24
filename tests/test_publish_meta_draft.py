from videoroll.apps.publish_meta_rules import apply_publish_source_overrides, build_bilibili_desc


def test_build_bilibili_desc_prepends_source_once() -> None:
    desc = build_bilibili_desc(
        "原视频：https://youtube.com/watch?v=abc\n\nsecond line",
        "https://youtube.com/watch?v=abc",
    )
    assert desc == "原视频：https://youtube.com/watch?v=abc\n\nsecond line"


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
        title_prefix="【熟肉】",
        enable_reprint=True,
    )

    assert meta["title"] == "【熟肉】中文标题"
    assert meta["desc"] == "原视频：https://youtube.com/watch?v=abc\n\nyoutube desc"
    assert meta["copyright"] == 2
    assert meta["source"] == "https://youtube.com/watch?v=abc"
