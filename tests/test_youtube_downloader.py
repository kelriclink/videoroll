from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

from yt_dlp.utils import DownloadError

from videoroll.apps.orchestrator_api import youtube_downloader as yd


@dataclass
class _Settings:
    youtube_user_agent: str = "UA/1.0"
    youtube_cookie_file: str | None = None
    youtube_proxy: str | None = None
    youtube_extractor_args_json: str | None = None
    youtube_compatibility_mode_enabled: bool = False
    ffmpeg_path: str = "ffmpeg"


class YouTubeDownloaderTests(unittest.TestCase):
    def test_download_retries_with_fallback_clients_when_format_unavailable(self) -> None:
        settings = _Settings()
        progress_hook = Mock()
        expected = (
            Path("/tmp/video.mp4"),
            {"id": "demo1234567", "title": "Demo"},
            yd.YouTubeMeta(title="Demo", description="", webpage_url="https://www.youtube.com/watch?v=demo1234567"),
        )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            yd,
            "_download_once",
            side_effect=[
                DownloadError("Requested format is not available. Use --list-formats for a list of available formats"),
                expected,
            ],
        ) as mock_download:
            result = yd.download_youtube_video(
                "https://www.youtube.com/watch?v=demo1234567",
                settings,
                work_dir=Path(tmp),
                progress_hook=progress_hook,
            )

        self.assertEqual(result, expected)
        self.assertEqual(mock_download.call_count, 2)
        self.assertIsNone(mock_download.call_args_list[0].kwargs.get("extractor_args_override"))
        self.assertEqual(
            mock_download.call_args_list[1].kwargs.get("extractor_args_override"),
            yd._FORMAT_UNAVAILABLE_FALLBACKS[0][1],
        )
        self.assertTrue(all(call.kwargs.get("progress_hook") is progress_hook for call in mock_download.call_args_list))

    def test_download_does_not_retry_on_unrelated_error(self) -> None:
        settings = _Settings()

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            yd,
            "_download_once",
            side_effect=DownloadError("network timeout"),
        ) as mock_download:
            with self.assertRaises(yd.YtDlpRuntimeError) as ctx:
                yd.download_youtube_video(
                    "https://www.youtube.com/watch?v=demo1234567",
                    settings,
                    work_dir=Path(tmp),
                )

        self.assertIn("network timeout", str(ctx.exception))
        self.assertEqual(mock_download.call_count, 1)

    def test_compatibility_mode_uses_community_player_clients(self) -> None:
        settings = _Settings(
            youtube_extractor_args_json='{"youtube":{"skip":["translated_subs"],"player_client":["tv"]}}',
            youtube_compatibility_mode_enabled=True,
        )

        opts = yd.build_ydl_opts(settings, for_download=False)

        self.assertEqual(opts["extractor_args"]["youtube"]["player_client"], ["default", "web_embedded"])
        self.assertEqual(opts["extractor_args"]["youtube"]["skip"], ["translated_subs"])

    def test_compatibility_mode_does_not_fall_back_to_tv_clients(self) -> None:
        settings = _Settings(youtube_compatibility_mode_enabled=True)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            yd,
            "_download_once",
            side_effect=DownloadError("Requested format is not available"),
        ) as mock_download:
            with self.assertRaises(yd.YtDlpRuntimeError):
                yd.download_youtube_video(
                    "https://www.youtube.com/watch?v=demo1234567",
                    settings,
                    work_dir=Path(tmp),
                )

        self.assertEqual(mock_download.call_count, 1)

    def test_detects_requested_format_unavailable_message(self) -> None:
        self.assertTrue(
            yd._looks_like_requested_format_unavailable(
                "ERROR: [youtube] abc123: Requested format is not available. Use --list-formats for a list of available formats"
            )
        )
        self.assertFalse(yd._looks_like_requested_format_unavailable("ERROR: network timeout"))


if __name__ == "__main__":
    unittest.main()
