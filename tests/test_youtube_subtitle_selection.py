from __future__ import annotations

import sys
import types
from unittest import TestCase

try:
    import httpx as _httpx  # type: ignore
except ModuleNotFoundError:
    fake_httpx = types.ModuleType("httpx")

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    fake_httpx.Client = Client
    sys.modules["httpx"] = fake_httpx

try:
    import yt_dlp as _yt_dlp  # type: ignore
except ModuleNotFoundError:
    fake_yt_dlp = types.ModuleType("yt_dlp")

    class YoutubeDL:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    fake_yt_dlp.YoutubeDL = YoutubeDL

    fake_utils = types.ModuleType("yt_dlp.utils")

    class DownloadError(Exception):
        pass

    fake_utils.DownloadError = DownloadError
    sys.modules["yt_dlp"] = fake_yt_dlp
    sys.modules["yt_dlp.utils"] = fake_utils

from videoroll.apps.orchestrator_api.youtube_downloader import pick_preferred_youtube_subtitle


class YouTubeSubtitleSelectionTests(TestCase):
    def test_prefers_target_manual_subtitle(self) -> None:
        info = {
            "subtitles": {
                "zh-Hans": [{"ext": "vtt"}],
                "en": [{"ext": "vtt"}],
            },
            "automatic_captions": {
                "en": [{"ext": "vtt"}],
            },
        }

        picked = pick_preferred_youtube_subtitle(info, target_lang="zh", mode="target")

        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.language, "zh-Hans")
        self.assertEqual(picked.source, "subtitles")
        self.assertEqual(picked.reason, "target")

    def test_falls_back_to_target_auto_subtitle(self) -> None:
        info = {
            "subtitles": {
                "en": [{"ext": "vtt"}],
            },
            "automatic_captions": {
                "ja": [{"ext": "vtt"}],
                "en": [{"ext": "vtt"}],
            },
        }

        picked = pick_preferred_youtube_subtitle(info, target_lang="ja-JP", mode="target")

        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.language, "ja")
        self.assertEqual(picked.source, "automatic_captions")
        self.assertEqual(picked.reason, "target")

    def test_target_mode_returns_none_when_target_missing(self) -> None:
        info = {
            "subtitles": {
                "en-US": [{"ext": "vtt"}],
            },
            "automatic_captions": {},
        }

        picked = pick_preferred_youtube_subtitle(info, target_lang="zh", mode="target")

        self.assertIsNone(picked)

    def test_returns_none_when_no_target_or_english_subtitle_exists(self) -> None:
        info = {
            "subtitles": {
                "fr": [{"ext": "vtt"}],
            },
            "automatic_captions": {
                "de": [{"ext": "vtt"}],
            },
        }

        picked = pick_preferred_youtube_subtitle(info, target_lang="zh", mode="target")

        self.assertIsNone(picked)

    def test_auto_source_mode_prefers_original_language_hint(self) -> None:
        info = {
            "language": "ja",
            "subtitles": {
                "zh-Hans": [{"ext": "vtt"}],
            },
            "automatic_captions": {
                "en": [{"ext": "vtt"}],
                "ja": [{"ext": "vtt"}],
            },
        }

        picked = pick_preferred_youtube_subtitle(info, target_lang="zh", mode="auto_source")

        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.language, "ja")
        self.assertEqual(picked.source, "automatic_captions")
        self.assertEqual(picked.reason, "auto_source")

    def test_auto_source_mode_uses_only_available_auto_caption(self) -> None:
        info = {
            "automatic_captions": {
                "ko": [{"ext": "vtt"}],
            },
        }

        picked = pick_preferred_youtube_subtitle(info, target_lang="zh", mode="auto_source")

        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.language, "ko")
        self.assertEqual(picked.source, "automatic_captions")
        self.assertEqual(picked.reason, "auto_source")
