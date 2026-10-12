from __future__ import annotations

import pytest

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

def test_critical_publish_commit_precedes_collection_enqueue() -> None:
    import inspect
    from videoroll.apps.bilibili_publisher.worker import _process_job_impl
    source = inspect.getsource(_process_job_impl)
    first_published = source.index("job.state = PublishState.published")
    durable_commit = source.index("db.commit()", first_published)
    async_enqueue = source.index("sync_collection.apply_async(")
    assert first_published < durable_commit < async_enqueue
    assert "_attach_youtube_uploader_collection(" not in source


def test_collection_network_calls_are_separate_from_db_session() -> None:
    import inspect
    from videoroll.apps.bilibili_publisher.worker import sync_collection
    source = inspect.getsource(sync_collection)
    assert source.index("with _db() as db:") < source.index("with BilibiliWebClient(cookie)")
    assert "Session has closed before all remote HTTP" in source


def test_collection_already_assigned_does_not_attach_twice() -> None:
    import uuid
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.worker import _attach_youtube_uploader_collection
    client = MagicMock()
    client.get_archive_season.return_value = {"season": {"id": 42, "title": "Test Channel"}}
    meta = MagicMock()
    r = _attach_youtube_uploader_collection(
        client=client, info={"uploader": "Test Channel"}, task_id=uuid.uuid4(),
        meta=meta, uploaded_cid=123, aid=456, csrf="test", cover_url="https://example.com/cover",
    )
    assert r["ok"] is True and r["already_assigned"] is True
    client.add_to_season.assert_not_called()
    client.ensure_season.assert_not_called()

def test_existing_unrelated_collection_is_not_overwritten() -> None:
    import uuid
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.worker import _attach_youtube_uploader_collection
    client = MagicMock()
    client.get_archive_season.return_value = {"season": {"id": 99, "title": "Manually Chosen"}}
    result = _attach_youtube_uploader_collection(
        client=client, info={"uploader": "Different Creator"},
        task_id=uuid.uuid4(), meta=MagicMock(), uploaded_cid=123,
        aid=456, csrf="test", cover_url="https://example.com/a.jpg",
    )
    assert result["skipped"] is True
    assert result["reason"] == "different_collection_already_assigned"
    client.add_to_season.assert_not_called()
    client.ensure_season.assert_not_called()


def test_collection_archive_lookup_unassigned_not_confused_with_platform_error() -> None:
    import httpx
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.bilibili_web_client import (
        BilibiliWebClient, BilibiliWebError,
    )
    client = BilibiliWebClient.__new__(BilibiliWebClient)
    client._bili = MagicMock()
    client._bili.get.return_value = httpx.Response(200, json={"code": -404, "message": "不存在"})
    assert client.get_archive_season(aid=456) is None
    client._bili.get.return_value = httpx.Response(200, json={"code": 0, "data": {"season": {"id": 99}}})
    assert client.get_archive_season(aid=456) == {"season": {"id": 99}}
    client._bili.get.return_value = httpx.Response(200, json={"code": -400, "message": "操作异常"})
    with pytest.raises(BilibiliWebError):
        client.get_archive_season(aid=456)

def test_no_existing_season_when_success_response_has_null_season() -> None:
    import uuid
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.worker import _attach_youtube_uploader_collection
    client = MagicMock()
    client.get_archive_season.return_value = {"season": None}
    client.find_season_by_title.return_value = MagicMock(season_id=88, section_id=22)
    client.add_to_season.return_value = {"code": 0}
    result = _attach_youtube_uploader_collection(
        client=client, info={"uploader": "Example Creator"}, task_id=uuid.uuid4(),
        meta=MagicMock(title="Translated Video"), uploaded_cid=123,
        aid=456, csrf="test", cover_url="https://example.com/cover",
    )
    assert result["ok"] is True
    client.add_to_season.assert_called_once()


def test_author_season_cover_uses_existing_bilibili_cover_without_channel_fetch() -> None:
    import uuid
    from videoroll.apps.bilibili_publisher.worker import _pick_youtube_collection_cover
    cover = "https://archive.biliimg.com/bfs/archive/cover.jpg"
    result, metadata = _pick_youtube_collection_cover(
        task_id=uuid.uuid4(),
        info={"uploader": "Example Creator", "channel_url": "https://www.youtube.com/@example"},
        fallback_cover_url=cover,
    )
    assert result == cover
    assert metadata == {"source": "video_cover"}

def test_create_season_omits_empty_optional_fields() -> None:
    import httpx
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient
    client = BilibiliWebClient.__new__(BilibiliWebClient)
    client._bili = MagicMock()
    client._bili.post.return_value = httpx.Response(
        200, json={"code": 0, "message": "OK", "data": 9307439}
    )
    created = client.create_season(
        title=" NASA Goddard ", cover="https://archive.biliimg.com/a.jpg",
        csrf="dummy", desc="", season_price=0,
    )
    assert created == 9307439
    assert client._bili.post.call_args.kwargs["data"] == {
        "title": "NASA Goddard",
        "cover": "https://archive.biliimg.com/a.jpg",
        "csrf": "dummy",
    }


def test_create_season_keeps_explicit_nonempty_options() -> None:
    import httpx
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient
    client = BilibiliWebClient.__new__(BilibiliWebClient)
    client._bili = MagicMock()
    client._bili.post.return_value = httpx.Response(200, json={"code": 0, "data": 88})
    client.create_season(
        title="Example", cover="https://archive.biliimg.com/a.jpg",
        csrf="dummy", desc=" Description ", season_price=7,
    )
    assert client._bili.post.call_args.kwargs["data"] == {
        "title": "Example", "cover": "https://archive.biliimg.com/a.jpg",
        "csrf": "dummy", "desc": "Description", "season_price": 7,
    }


def test_existing_season_direct_object_response_is_idempotent() -> None:
    import uuid
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.worker import _attach_youtube_uploader_collection
    client = MagicMock()
    client.get_archive_season.return_value = {
        "id": 9307439, "title": "NASA Goddard", "state": 0
    }
    result = _attach_youtube_uploader_collection(
        client=client, info={"uploader": "NASA Goddard"},
        task_id=uuid.uuid4(), meta=MagicMock(), uploaded_cid=42,
        aid=117426268146290, csrf="dummy", cover_url="",
    )
    assert result["ok"] is True and result["already_assigned"] is True
    assert result["season_id"] == 9307439
    client.add_to_season.assert_not_called()
    client.ensure_season.assert_not_called()


def test_existing_different_direct_season_not_overwritten() -> None:
    import uuid
    from unittest.mock import MagicMock
    from videoroll.apps.bilibili_publisher.worker import _attach_youtube_uploader_collection
    client = MagicMock()
    client.get_archive_season.return_value = {
        "id": 99, "title": "Manually Picked Collection", "state": 0
    }
    result = _attach_youtube_uploader_collection(
        client=client, info={"uploader": "NASA Goddard"},
        task_id=uuid.uuid4(), meta=MagicMock(), uploaded_cid=42,
        aid=117426268146290, csrf="dummy", cover_url="",
    )
    assert result["skipped"] is True
    assert result["reason"] == "different_collection_already_assigned"
    client.add_to_season.assert_not_called()
