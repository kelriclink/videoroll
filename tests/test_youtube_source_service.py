from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

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
    scan_youtube_source_by_id,
    source_to_read_dict,
    upsert_youtube_source,
    youtube_source_is_due,
)
from videoroll.apps.youtube_ingest.youtube_feed import FeedEntry
from videoroll.db.base import Base
from videoroll.db.models import IngestedVideo, SourceLicense, Task, YouTubeSource, YouTubeSourceType


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

        fake_worker = types.ModuleType("videoroll.apps.subtitle_service.worker")
        fake_worker.celery_app = fake_celery
        with patch.dict(sys.modules, {"videoroll.apps.subtitle_service.worker": fake_worker}):
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

    def test_resolve_raw_playlist_id(self) -> None:
        resolved = resolve_youtube_source_input("PL1234567890ABCDE", user_agent="UA/1.0")

        self.assertEqual(resolved.source_type, YouTubeSourceType.playlist)
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

    def test_playlist_uses_the_same_scan_interval_as_channel_sources(self) -> None:
        now = datetime.now(timezone.utc)
        src = YouTubeSource(
            source_type=YouTubeSourceType.playlist,
            source_id="PL1234567890ABCDE",
            enabled=True,
            scan_interval_minutes=30,
            last_scan_finished_at=now - timedelta(minutes=31),
        )

        self.assertTrue(youtube_source_is_due(src, now=now))

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

    def test_prepare_scan_entries_since_requires_a_known_newer_date(self) -> None:
        cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
        entries = [
            FeedEntry(video_id="unknown", title="Unknown date", published_at=None),
            FeedEntry(video_id="older", title="Older", published_at=cutoff - timedelta(seconds=1)),
            FeedEntry(video_id="at-cutoff", title="At cutoff", published_at=cutoff),
            FeedEntry(video_id="newer", title="Newer", published_at=cutoff + timedelta(seconds=1)),
        ]

        discovered, selected, skipped = _prepare_scan_entries(
            entries,
            existing_video_ids=set(),
            create_limit=10,
            since=cutoff,
        )

        self.assertEqual([entry.video_id for entry in discovered], ["newer"])
        self.assertEqual([entry.video_id for entry in selected], ["newer"])
        self.assertEqual(skipped, 0)


class YouTubePlaylistScanTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            self.engine,
            tables=[Task.__table__, YouTubeSource.__table__, IngestedVideo.__table__],
        )
        self.db = Session(self.engine)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _playlist_entries(self) -> list[FeedEntry]:
        now = datetime.now(timezone.utc)
        return [
            FeedEntry(
                video_id=f"playlist-video-{idx}",
                title=f"Playlist video {idx}",
                published_at=now - timedelta(minutes=idx),
            )
            for idx in range(5)
        ]

    def test_playlist_scan_enumerates_all_and_backfills_by_scan_limit(self) -> None:
        source = YouTubeSource(
            source_type=YouTubeSourceType.playlist,
            source_id="PL1234567890ABCDE",
            source_url="https://www.youtube.com/playlist?list=PL1234567890ABCDE",
            license=SourceLicense.authorized,
            enabled=True,
            scan_interval_minutes=30,
            scan_limit=2,
            auto_process=True,
        )
        self.db.add(source)
        self.db.commit()
        self.db.refresh(source)

        with (
            patch("videoroll.apps.youtube_ingest.source_service.fetch_youtube_feed", return_value=self._playlist_entries()) as fetch_feed,
            patch("videoroll.apps.youtube_ingest.source_service.get_auto_profile", return_value={"auto_publish": False}),
            patch("videoroll.apps.youtube_ingest.source_service.get_youtube_settings", return_value={}),
            patch("videoroll.apps.youtube_ingest.source_service._start_auto_pipeline", side_effect=["job-1", "job-2", "job-3", "job-4", "job-5"]),
        ):
            first = scan_youtube_source_by_id(self.db, source.id, user_agent="UA/1.0", force=True)
            second = scan_youtube_source_by_id(self.db, source.id, user_agent="UA/1.0", force=True)
            third = scan_youtube_source_by_id(self.db, source.id, user_agent="UA/1.0", force=True)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(third)
        assert first is not None and second is not None and third is not None
        self.assertEqual(first.discovered_count, 5)
        self.assertEqual(len(first.created_task_ids), 2)
        self.assertEqual(len(second.created_task_ids), 2)
        self.assertEqual(len(third.created_task_ids), 1)
        self.assertEqual(len(first.started_pipeline_job_ids), 2)
        self.assertEqual(len(second.started_pipeline_job_ids), 2)
        self.assertEqual(len(third.started_pipeline_job_ids), 1)
        self.assertEqual(self.db.query(IngestedVideo).count(), 5)
        self.assertEqual(self.db.query(Task).count(), 5)
        self.assertEqual(fetch_feed.call_count, 3)
        for call in fetch_feed.call_args_list:
            self.assertEqual(call.args[:2], ("playlist", "PL1234567890ABCDE"))
            self.assertIsNone(call.kwargs["limit"])

    def test_selected_source_type_must_match_resolved_url(self) -> None:
        with patch("videoroll.apps.youtube_ingest.source_service.get_youtube_settings", return_value={}):
            with self.assertRaisesRegex(ValueError, "resolved as playlist"):
                upsert_youtube_source(
                    self.db,
                    source_input="https://www.youtube.com/playlist?list=PL1234567890ABCDE",
                    source_type=YouTubeSourceType.channel,
                    source_id=None,
                    license=SourceLicense.authorized,
                    proof_url=None,
                    enabled=True,
                    scan_interval_minutes=60,
                    scan_limit=20,
                    auto_process=True,
                    user_agent="UA/1.0",
                )

    def test_scan_without_since_ingests_unknown_dates_in_source_order(self) -> None:
        source = YouTubeSource(
            source_type=YouTubeSourceType.playlist,
            source_id="PL1234567890ABCDE",
            license=SourceLicense.authorized,
            enabled=True,
            scan_limit=1,
            auto_process=False,
        )
        self.db.add(source)
        self.db.commit()
        entries = [
            FeedEntry(video_id="first", title="First", published_at=None),
            FeedEntry(video_id="second", title="Second", published_at=None),
        ]

        with (
            patch("videoroll.apps.youtube_ingest.source_service.fetch_youtube_feed", return_value=entries),
            patch("videoroll.apps.youtube_ingest.source_service.get_youtube_settings", return_value={}),
        ):
            result = scan_youtube_source_by_id(self.db, source.id, user_agent="UA/1.0", force=True)

        assert result is not None
        self.assertEqual(len(result.created_task_ids), 1)
        ingested = self.db.query(IngestedVideo).one()
        self.assertEqual(ingested.source_id, "first")
        self.assertIsNone(ingested.published_at)

    def test_failed_scan_records_fetch_error_and_releases_lock(self) -> None:
        for source_type in (YouTubeSourceType.channel, YouTubeSourceType.playlist):
            with self.subTest(source_type=source_type):
                source = YouTubeSource(
                    source_type=source_type,
                    source_id="offline-source",
                    license=SourceLicense.authorized,
                    enabled=True,
                    auto_process=False,
                )
                self.db.add(source)
                self.db.commit()

                with (
                    patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
                    patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client") as client,
                    patch("videoroll.apps.youtube_ingest.source_service.get_youtube_settings", return_value={}),
                ):
                    ydl.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError("extractor blocked")
                    client.return_value.__enter__.return_value.get.side_effect = ConnectionError("RSS proxy unavailable")
                    with self.assertRaisesRegex(RuntimeError, "fetch youtube feed failed"):
                        scan_youtube_source_by_id(self.db, source.id, user_agent="UA/1.0", force=True)

                self.db.refresh(source)
                self.assertIn("extractor blocked", source.last_scan_error or "")
                self.assertIn("RSS proxy unavailable", source.last_scan_error or "")
                self.assertIsNone(source.scan_lock_owner)
                self.assertIsNone(source.scan_lock_until)
                self.assertIsNotNone(source.last_scan_finished_at)
                self.assertEqual(source.last_scan_created_count, 0)
                self.assertEqual(self.db.query(Task).count(), 0)

    def test_successful_empty_scan_clears_a_previous_error(self) -> None:
        source = YouTubeSource(
            source_type=YouTubeSourceType.playlist,
            source_id="empty-source",
            license=SourceLicense.authorized,
            enabled=True,
            auto_process=False,
            last_scan_error="previous scan failed",
        )
        self.db.add(source)
        self.db.commit()

        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client") as client,
            patch("videoroll.apps.youtube_ingest.source_service.get_youtube_settings", return_value={}),
        ):
            ydl.return_value.__enter__.return_value.extract_info.return_value = {"entries": []}
            client.return_value.__enter__.return_value.get.side_effect = ConnectionError("RSS proxy unavailable")
            result = scan_youtube_source_by_id(self.db, source.id, user_agent="UA/1.0", force=True)

        assert result is not None
        self.assertEqual(result.discovered_count, 0)
        self.db.refresh(source)
        self.assertIsNone(source.last_scan_error)
        self.assertIsNone(source.scan_lock_until)
