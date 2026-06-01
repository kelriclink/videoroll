from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from videoroll.apps.youtube_ingest.youtube_feed import fetch_youtube_feed


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


class YouTubeFeedTests(TestCase):
    def test_fetch_youtube_feed_prefers_ytdlp_when_limit_exceeds_rss_cap(self) -> None:
        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FakeHttpxClient),
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FakeYdl),
        ):
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0", limit=20))

        self.assertEqual(len(entries), 20)
        self.assertEqual(entries[0].video_id, "yt-000")
        self.assertEqual(entries[-1].video_id, "yt-019")

    def test_fetch_youtube_feed_falls_back_to_rss_when_ytdlp_fails(self) -> None:
        with (
            patch("videoroll.apps.youtube_ingest.youtube_feed.httpx.Client", _FakeHttpxClient),
            patch("videoroll.apps.youtube_ingest.youtube_feed.yt_dlp.YoutubeDL", _FailingYdl),
        ):
            entries = list(fetch_youtube_feed("channel", "UCexample1234567890", user_agent="UA/1.0", limit=20))

        self.assertEqual(len(entries), 15)
        self.assertEqual(entries[0].video_id, "rss-000")
        self.assertEqual(entries[-1].video_id, "rss-014")
