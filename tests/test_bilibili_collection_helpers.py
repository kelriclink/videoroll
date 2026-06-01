from __future__ import annotations

from videoroll.apps.bilibili_publisher.worker import (
    _normalize_collection_title,
    _youtube_channel_url_from_info,
    _youtube_collection_title_from_info,
)
from videoroll.apps.orchestrator_api.youtube_downloader import pick_thumbnail_url


def test_normalize_collection_title_collapses_whitespace_and_clamps() -> None:
    raw = "  Usagi \n   Electric\t" + ("x" * 100)
    title = _normalize_collection_title(raw, max_chars=20)
    assert title == "Usagi Electric xxxxx"


def test_youtube_collection_title_prefers_uploader_name() -> None:
    info = {
        "uploader": "Usagi Electric",
        "channel": "Ignored Channel",
        "uploader_id": "@UsagiElectric",
    }
    assert _youtube_collection_title_from_info(info) == "Usagi Electric"


def test_youtube_collection_title_falls_back_to_channel_handle() -> None:
    info = {
        "uploader": "",
        "channel": "",
        "uploader_id": "@UsagiElectric",
    }
    assert _youtube_collection_title_from_info(info) == "@UsagiElectric"


def test_youtube_channel_url_prefers_handle_url() -> None:
    info = {
        "uploader_url": "https://www.youtube.com/@UsagiElectric",
        "channel_url": "https://www.youtube.com/channel/UCE4xstUnu0YmkG-W9_PyYrQ",
    }
    assert _youtube_channel_url_from_info(info) == "https://www.youtube.com/@UsagiElectric"


def test_pick_thumbnail_url_prefers_largest_channel_banner() -> None:
    info = {
        "thumbnails": [
            {"url": "https://img.example/avatar.jpg", "width": 160, "height": 160},
            {"url": "https://img.example/banner-small.jpg", "width": 1060, "height": 175},
            {"url": "https://img.example/banner-large.jpg", "width": 2120, "height": 351},
        ]
    }
    assert pick_thumbnail_url(info) == "https://img.example/banner-large.jpg"
