from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

import httpx

from videoroll.apps.youtube_ingest.youtube_feed import _fetch_channel_uploads_ytdlp, fetch_youtube_feed


def _build_rss_xml(count: int) -> str:
    parts = []
    for idx in range(count):
        parts.append(
            (
                "<entry>"
                f"<yt:videoId>rss-{idx:03d}</yt:videoId>"
                f"<title>RSS Video {idx}</title>"
                f"<published>2026-04-{(idx % 9) + 1:02d}T12:00:00+00:00</published>"
                "</entry>"
            )
        )
    inner = "".join(parts)
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
        f"{inner}"
        "</feed>"
    )


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeHttpxClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, _url: str) -> _FakeResponse:
        return _FakeResponse(_build_rss_xml(15))


class _FailingHttpxClient(_FakeHttpxClient):
    def get(self, _url: str) -> _FakeResponse:
        raise httpx.ConnectError("RSS unavailable")


class _FakeYdl:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeYdl":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, download: bool = False) -> dict[str, object]:
        assert download is False
        return {
            "entries": [
                {
                    "id": f"yt-{idx:03d}",
                    "title": f"YTDLP Video {idx}",
                    "timestamp": 1_700_000_000 + idx,
                }
                for idx in range(20)
            ]
        }


class _FailingYdl(_FakeYdl):
    def extract_info(self, _url: str, download: bool = False) -> dict[str, object]:
        assert download is False
        raise RuntimeError("yt-dlp failed")


class _ChannelTabsYdl(_FakeYdl):
    def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
        assert download is False
        if url.endswith("/videos"):
            return {
                "entries": [
                    {"id": "regular-new", "title": "Regular newest", "timestamp": 1_700_000_200},
                    {"id": "shared", "title": "Shared", "timestamp": 1_700_000_100},
                ]
            }
        if url.endswith("/shorts"):
            return {
                "entries": [
                    {"id": "short-new", "title": "Short newest", "timestamp": 1_700_000_300},
                    {"id": "shared", "title": "Shared duplicate", "timestamp": 1_700_000_100},
                ]
            }
        raise AssertionError(f"unexpected URL: {url}")


class _FullPlaylistYdl(_FakeYdl):
    urls: list[str] = []
    options: list[dict[str, object]] = []

    def __init__(self, opts: dict[str, object]) -> None:
        type(self).options.append(dict(opts))

    def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
        assert download is False
        type(self).urls.append(url)
        return {
            "entries": [
                {
                    "id": f"playlist-{idx:03d}",
                    "title": f"Playlist Video {idx}",
                    "timestamp": 1_700_000_000 + idx,
                }
                for idx in range(37)
            ]
        }


class _UndatedChannelTabsYdl(_FakeYdl):
    def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
        assert download is False
        if url.endswith("/videos"):
            ids = ["regular-newest", "regular-oldest"]
        elif url.endswith("/shorts"):
            ids = ["short-newest", "short-oldest"]
        else:
            raise AssertionError(f"unexpected URL: {url}")
        return {"entries": [{"id": video_id, "title": video_id, "timestamp": None} for video_id in ids]}


class _MixedDateChannelTabsYdl(_FakeYdl):
    def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
        assert download is False
        if url.endswith("/videos"):
            return {"entries": [
                {"id": "undated-newest", "title": "Unknown date"},
                {"id": "dated-older", "title": "Older", "timestamp": 1_700_000_100},
                {"id": "undated-oldest", "title": "Unknown date", "upload_date": "invalid"},
            ]}
        if url.endswith("/shorts"):
            return {"entries": [
                {"id": "dated-newer", "title": "Newer", "timestamp": 1_700_000_200},
                {"id": "undated-short", "title": "Unknown date"},
            ]}
        raise AssertionError(f"unexpected URL: {url}")


class YouTubeFeedTests(TestCase):
    def setUp(self) -> None:
        _FullPlaylistYdl.urls.clear()
        _FullPlaylistYdl.options.clear()

    def test_fetch_youtube_feed_prefers_ytdlp_when_limit_exceeds_rss_cap(self) -> None:
        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FakeHttpxClient),
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FakeYdl),
        ):
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0", limit=20))

        self.assertEqual(len(entries), 20)
        self.assertEqual(entries[0].video_id, "yt-019")
        self.assertEqual(entries[-1].video_id, "yt-000")

    def test_fetch_youtube_feed_falls_back_to_rss_when_ytdlp_fails(self) -> None:
        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FakeHttpxClient),
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FailingYdl),
        ):
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0", limit=20))

        self.assertEqual(len(entries), 15)
        self.assertEqual(entries[0].video_id, "rss-000")
        self.assertEqual(entries[-1].video_id, "rss-014")

    def test_channel_feed_merges_videos_and_shorts_newest_first(self) -> None:
        with patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _ChannelTabsYdl):
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0"))

        self.assertEqual([entry.video_id for entry in entries], ["short-new", "regular-new", "shared"])

    def test_playlist_feed_enumerates_the_complete_playlist_when_limit_is_none(self) -> None:
        with patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FullPlaylistYdl):
            entries = list(fetch_youtube_feed("playlist", "PLexample1234567890", user_agent="UA/1.0", limit=None))

        self.assertEqual(len(entries), 37)
        self.assertEqual(_FullPlaylistYdl.urls, ["https://www.youtube.com/playlist?list=PLexample1234567890"])
        self.assertNotIn("playlistend", _FullPlaylistYdl.options[0])

    def test_channel_feed_preserves_source_order_without_publication_dates(self) -> None:
        with patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _UndatedChannelTabsYdl):
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0"))

        self.assertEqual([entry.video_id for entry in entries], ["regular-newest", "regular-oldest", "short-newest", "short-oldest"])
        self.assertTrue(all(entry.published_at is None for entry in entries))

    def test_channel_feed_sorts_known_dates_and_keeps_undated_entries_stable(self) -> None:
        with patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _MixedDateChannelTabsYdl):
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0"))

        self.assertEqual([entry.video_id for entry in entries], ["dated-newer", "dated-older", "undated-newest", "undated-oldest", "undated-short"])
        self.assertTrue(all(entry.published_at is None for entry in entries[2:]))

    def test_fetch_youtube_feed_reports_all_failed_loaders(self) -> None:
        for source_type, source_id, limit in (("channel", "UCexample1234567890", None), ("playlist", "PLexample1234567890", None), ("playlist", "PLexample1234567890", 10)):
            with (
                self.subTest(source_type=source_type, limit=limit),
                patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FailingYdl),
                patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FailingHttpxClient),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    list(fetch_youtube_feed(source_type, source_id, user_agent="UA/1.0", limit=limit))

                self.assertIn("yt-dlp failed", str(raised.exception))
                self.assertIn("RSS unavailable", str(raised.exception))

    def test_successful_empty_ytdlp_source_is_not_a_failure(self) -> None:
        for source_type in ("channel", "playlist"):
            with (
                self.subTest(source_type=source_type),
                patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
                patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FailingHttpxClient),
            ):
                ydl.return_value.__enter__.return_value.extract_info.return_value = {"entries": []}
                entries = list(fetch_youtube_feed(source_type, "empty-source", user_agent="UA/1.0"))

                self.assertEqual(entries, [])

    def test_successful_empty_rss_source_is_not_a_failure(self) -> None:
        for source_type in ("channel", "playlist"):
            with (
                self.subTest(source_type=source_type),
                patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FailingYdl),
                patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client") as client,
            ):
                client.return_value.__enter__.return_value.get.return_value = _FakeResponse(_build_rss_xml(0))
                entries = list(fetch_youtube_feed(source_type, "empty-source", user_agent="UA/1.0"))

                self.assertEqual(entries, [])

    def test_channel_feed_accepts_a_successful_tab_when_the_other_is_missing(self) -> None:
        for missing_tab in ("videos", "shorts"):
            for tab_entries in ([], [{"id": "available-video", "title": "Available"}]):
                with (
                    self.subTest(missing_tab=missing_tab, empty=not tab_entries),
                    patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
                    patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FailingHttpxClient),
                ):
                    def extract_info(url: str, download: bool = False) -> dict[str, object]:
                        if url.endswith(f"/{missing_tab}"):
                            raise RuntimeError(f"channel has no {missing_tab} tab")
                        return {"entries": tab_entries}

                    ydl.return_value.__enter__.return_value.extract_info.side_effect = extract_info
                    entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0"))

                    self.assertEqual([entry.video_id for entry in entries], ["available-video"] if tab_entries else [])

    def test_playlist_feed_falls_back_to_rss_when_ytdlp_fails(self) -> None:
        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FailingYdl),
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FakeHttpxClient),
        ):
            entries = list(fetch_youtube_feed("playlist", "PLexample1234567890", user_agent="UA/1.0"))

        self.assertEqual(len(entries), 15)
        self.assertEqual(entries[0].video_id, "rss-000")

    def test_playlist_feed_falls_back_to_ytdlp_when_rss_fails(self) -> None:
        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FailingHttpxClient),
        ):
            ydl.return_value.__enter__.return_value.extract_info.return_value = {
                "entries": [{"id": "yt-fallback", "title": "Fallback"}],
            }
            entries = list(fetch_youtube_feed("playlist", "PLexample1234567890", user_agent="UA/1.0", limit=10))

        self.assertEqual([entry.video_id for entry in entries], ["yt-fallback"])

    def test_invalid_rss_response_is_not_a_successful_empty_feed(self) -> None:
        for response_text in ("", "not XML", "<html>Sign in</html>"):
            with (
                self.subTest(response_text=response_text),
                patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FailingYdl),
                patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client") as client,
            ):
                client.return_value.__enter__.return_value.get.return_value = _FakeResponse(response_text)
                with self.assertRaises(RuntimeError):
                    list(fetch_youtube_feed("playlist", "PLexample1234567890", user_agent="UA/1.0"))

    def test_invalid_ytdlp_response_is_not_a_successful_empty_feed(self) -> None:
        for response in (None, {}, {"entries": None}):
            with (
                self.subTest(response=response),
                patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
                patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FailingHttpxClient),
            ):
                ydl.return_value.__enter__.return_value.extract_info.return_value = response
                with self.assertRaises(RuntimeError):
                    list(fetch_youtube_feed("playlist", "PLexample1234567890", user_agent="UA/1.0"))

    def test_ytdlp_pagination_failure_is_not_a_successful_empty_feed(self) -> None:
        def broken_entries():
            yield {"id": "first-page", "title": "First page"}
            raise RuntimeError("playlist page failed")

        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FailingHttpxClient),
        ):
            ydl.return_value.__enter__.return_value.extract_info.return_value = {"entries": broken_entries()}
            with self.assertRaisesRegex(RuntimeError, "playlist page failed"):
                list(fetch_youtube_feed("playlist", "PLexample1234567890", user_agent="UA/1.0"))

    def test_feed_failure_messages_are_bounded_and_identify_loaders(self) -> None:
        for source_type, limit in (("channel", None), ("playlist", None), ("playlist", 10)):
            with (
                self.subTest(source_type=source_type, limit=limit),
                patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
                patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client") as client,
            ):
                ydl.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError("extractor failed " + "X" * 10_000)
                client.return_value.__enter__.return_value.get.side_effect = ConnectionError("RSS failed " + "Y" * 10_000)
                with self.assertRaises(RuntimeError) as raised:
                    list(fetch_youtube_feed(source_type, "offline-source", user_agent="UA/1.0", limit=limit))

                message = str(raised.exception)
                self.assertLessEqual(len(message), 2_000)
                self.assertIn("yt-dlp:", message)
                self.assertIn("rss:", message)
                self.assertNotIn("X" * 501, message)
                self.assertNotIn("Y" * 501, message)

    def test_channel_tab_failure_messages_are_bounded_and_identify_tabs(self) -> None:
        with patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = [
                RuntimeError("videos failed " + "V" * 10_000),
                RuntimeError("shorts failed " + "S" * 10_000),
            ]
            with self.assertRaises(RuntimeError) as raised:
                _fetch_channel_uploads_ytdlp("UCexample1234567890", "UA/1.0")

        message = str(raised.exception)
        self.assertLessEqual(len(message), 2_000)
        self.assertIn("videos:", message)
        self.assertIn("shorts:", message)
        self.assertNotIn("V" * 501, message)
        self.assertNotIn("S" * 501, message)

    def test_partial_channel_warning_is_bounded_and_identifies_the_failed_tab(self) -> None:
        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL") as ydl,
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FailingHttpxClient),
            self.assertLogs("videoroll.apps.youtube_ingest.youtube_feed", level="WARNING") as logs,
        ):
            ydl.return_value.__enter__.return_value.extract_info.side_effect = [
                {"entries": [{"id": "available-video", "title": "Available"}]},
                RuntimeError("shorts failed " + "S" * 10_000),
            ]
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0"))

        self.assertEqual([entry.video_id for entry in entries], ["available-video"])
        self.assertEqual(len(logs.records), 1)
        warning = logs.records[0].getMessage()
        self.assertLessEqual(len(warning), 2_000)
        self.assertIn("shorts:", warning)
        self.assertNotIn("S" * 501, warning)
