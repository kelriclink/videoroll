from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

try:
    import httpx as _httpx  # type: ignore
except ModuleNotFoundError:
    fake_httpx = types.ModuleType("httpx")

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    fake_httpx.Client = Client
    sys.modules["httpx"] = fake_httpx

from videoroll.apps.youtube_ingest.source_service import (
    _start_auto_pipeline,
    _prepare_scan_entries,
    resolve_youtube_source_input,
    source_to_read_dict,
    youtube_source_is_due,
)
from videoroll.apps.youtube_ingest.youtube_feed import FeedEntry
from videoroll.db.models import SourceLicense, YouTubeSource, YouTubeSourceType


class _FakeYdl:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeYdl":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, download: bool = False) -> dict[str, str]:
        assert download is False
        return {
            "channel_id": "UCresolved1234567890",
            "channel_url": "https://www.youtube.com/channel/UCresolved1234567890",
            "channel": "Resolved Creator",
        }


class _FakeAsyncResult:
    def __init__(self, job_id: str) -> None:
        self.id = job_id


class _FakeCeleryApp:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_task(self, name: str, *, args: list[object], queue: str) -> _FakeAsyncResult:
        self.calls.append({"name": name, "args": list(args), "queue": queue})
        return _FakeAsyncResult("job-123")


class YouTubeSourceServiceTests(TestCase):
    def test_start_auto_pipeline_passes_auto_publish_override(self) -> None:
        task_id = uuid4()
        fake_celery = _FakeCeleryApp()

        with patch("videoroll.apps.subtitle_service.worker.celery_app", fake_celery):
            job_id = _start_auto_pipeline(task_id, auto_publish=True)

        self.assertEqual(job_id, "job-123")
        self.assertEqual(
            fake_celery.calls,
            [
                {
                    "name": "subtitle_service.auto_youtube_pipeline",
                    "args": [str(task_id), {"auto_publish": True}],
                    "queue": "subtitle",
                }
            ],
        )

    def test_resolve_direct_channel_url(self) -> None:
        resolved = resolve_youtube_source_input(
            "https://www.youtube.com/channel/UCabc1234567890xyz",
            user_agent="UA/1.0",
        )

        self.assertEqual(resolved.source_type, YouTubeSourceType.channel)
        self.assertEqual(resolved.source_id, "UCabc1234567890xyz")
        self.assertEqual(resolved.source_url, "https://www.youtube.com/channel/UCabc1234567890xyz")

    def test_resolve_direct_playlist_url_is_canonicalized(self) -> None:
        resolved = resolve_youtube_source_input(
            "https://www.youtube.com/playlist?list=PL1234567890ABCDE&feature=share",
            user_agent="UA/1.0",
        )

        self.assertEqual(resolved.source_type, YouTubeSourceType.playlist)
        self.assertEqual(resolved.source_id, "PL1234567890ABCDE")
        self.assertEqual(resolved.source_url, "https://www.youtube.com/playlist?list=PL1234567890ABCDE")

    def test_resolve_handle_uses_ytdlp_fallback(self) -> None:
        with patch("videoroll.apps.youtube_ingest.source_service.yt_dlp.YoutubeDL", _FakeYdl):
            resolved = resolve_youtube_source_input("@creator", user_agent="UA/1.0")

        self.assertEqual(resolved.source_type, YouTubeSourceType.channel)
        self.assertEqual(resolved.source_id, "UCresolved1234567890")
        self.assertEqual(resolved.source_url, "https://www.youtube.com/channel/UCresolved1234567890")
        self.assertEqual(resolved.display_name, "Resolved Creator")

    def test_source_due_when_never_scanned(self) -> None:
        src = YouTubeSource(
            source_type=YouTubeSourceType.channel,
            source_id="UCabc1234567890xyz",
            enabled=True,
            scan_interval_minutes=30,
        )

        self.assertTrue(youtube_source_is_due(src, now=datetime.now(timezone.utc)))

    def test_source_not_due_before_interval(self) -> None:
        now = datetime.now(timezone.utc)
        src = YouTubeSource(
            source_type=YouTubeSourceType.channel,
            source_id="UCabc1234567890xyz",
            enabled=True,
            scan_interval_minutes=60,
            last_scan_finished_at=now - timedelta(minutes=10),
        )

        self.assertFalse(youtube_source_is_due(src, now=now))

    def test_source_to_read_dict_fills_defaults(self) -> None:
        now = datetime.now(timezone.utc)
        src = YouTubeSource(
            source_type=YouTubeSourceType.channel,
            source_id="UCabc1234567890xyz",
            license=SourceLicense.authorized,
            enabled=True,
            created_at=now,
            updated_at=now,
        )

        data = source_to_read_dict(src)

        self.assertEqual(data["source_url"], "https://www.youtube.com/channel/UCabc1234567890xyz")
        self.assertEqual(data["scan_interval_minutes"], 60)
        self.assertEqual(data["scan_limit"], 20)
        self.assertTrue(data["auto_process"])
        self.assertEqual(data["last_scan_discovered_count"], 0)

    def test_prepare_scan_entries_backfills_past_existing_newest_videos(self) -> None:
        now = datetime.now(timezone.utc)
        entries = [
            FeedEntry(video_id="vid-newest", title="Newest", published_at=now),
            FeedEntry(video_id="vid-second", title="Second", published_at=now - timedelta(minutes=1)),
            FeedEntry(video_id="vid-third", title="Third", published_at=now - timedelta(minutes=2)),
            FeedEntry(video_id="vid-fourth", title="Fourth", published_at=now - timedelta(minutes=3)),
        ]

        discovered, selected, skipped = _prepare_scan_entries(
            entries,
            existing_video_ids={"vid-newest", "vid-second"},
            create_limit=2,
        )

        self.assertEqual([entry.video_id for entry in discovered], ["vid-newest", "vid-second", "vid-third", "vid-fourth"])
        self.assertEqual([entry.video_id for entry in selected], ["vid-third", "vid-fourth"])
        self.assertEqual(skipped, 2)
