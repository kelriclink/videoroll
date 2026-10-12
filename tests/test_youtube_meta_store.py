from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from videoroll.apps.youtube_meta_store import snapshot_downloaded_youtube_info


def test_snapshot_downloaded_youtube_info_persists_uploader_before_cleanup() -> None:
    db = MagicMock()
    task_id = uuid.uuid4()
    info = {
        "id": "abc123", "title": "Science video",
        "uploader": "NASA Educational Channel",
        "channel": "Fallback channel",
        "description": "Publicly available science footage",
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "duration": 61.5, "upload_date": "20261012",
    }
    with patch("videoroll.apps.youtube_meta_store.upsert_task_youtube_meta", return_value=SimpleNamespace(uploader="NASA Educational Channel")) as spy:
        result = snapshot_downloaded_youtube_info(
            db, task_id, info, fallback_url="https://youtube.com/watch?v=abc123"
        )
    assert result.uploader == "NASA Educational Channel"
    kwargs = spy.call_args.kwargs
    assert kwargs["uploader"] == "NASA Educational Channel"
    assert kwargs["duration"] == 61
    assert kwargs["source_id"] == "abc123"
    assert kwargs["webpage_url"].endswith("abc123")


def test_snapshot_downloaded_youtube_info_handles_channel_and_missing_duration() -> None:
    with patch("videoroll.apps.youtube_meta_store.upsert_task_youtube_meta") as spy:
        snapshot_downloaded_youtube_info(
            MagicMock(), uuid.uuid4(), {"channel": "Research Channel", "duration": "unknown"},
            fallback_url="https://www.youtube.com/watch?v=xyz",
        )
    assert spy.call_args.kwargs["uploader"] == "Research Channel"
    assert spy.call_args.kwargs["duration"] is None


def test_snapshot_downloaded_youtube_info_skips_empty_metadata() -> None:
    with patch("videoroll.apps.youtube_meta_store.upsert_task_youtube_meta") as spy:
        assert snapshot_downloaded_youtube_info(
            MagicMock(), uuid.uuid4(), {}, fallback_url="https://youtube.com"
        ) is None
    spy.assert_not_called()
