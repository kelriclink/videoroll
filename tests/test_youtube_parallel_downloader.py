"""YouTube direct HTTPS Range concurrency, progress, and durable fallback tests."""
from __future__ import annotations

import re
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from yt_dlp import YoutubeDL
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.utils import DownloadError

from videoroll.apps.orchestrator_api import youtube_downloader as yd
from videoroll.apps.orchestrator_api.youtube_parallel_downloader import (
    VideoRollAria2cFD, parse_aria2_progress, register_parallel_downloader,
)
from videoroll.apps.youtube_settings_store import (
    get_youtube_settings, update_youtube_settings,
)
from videoroll.db.models import AppSetting


@dataclass
class Settings:
    youtube_user_agent: str = "VideoRoll-test"
    youtube_proxy: str | None = None
    youtube_cookie_file: str | None = None
    youtube_extractor_args_json: str | None = None
    youtube_compatibility_mode_enabled: bool = False
    ffmpeg_path: str = "ffmpeg"
    youtube_parallel_download_enabled: bool = True
    youtube_parallel_download_connections: int = 8


def test_settings_persist_and_validate_parallel_connections():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    AppSetting.__table__.create(engine)
    with Session(engine) as db:
        before = get_youtube_settings(db)
        assert before["parallel_download_enabled"] is True
        assert before["parallel_download_connections"] == 8
        after = update_youtube_settings(db, {
            "parallel_download_enabled": False,
            "parallel_download_connections": 5,
        })
        assert after["parallel_download_enabled"] is False
        assert after["parallel_download_connections"] == 5
        with pytest.raises(ValueError, match="between 1 and 16"):
            update_youtube_settings(db, {"parallel_download_connections": 17})


def test_format_selector_only_chooses_aria_for_big_http_objects():
    if not register_parallel_downloader():
        pytest.skip("aria2c not installed on this test runner")
    settings = Settings()
    opts = yd.build_ydl_opts(settings, for_download=True)
    assert opts["videoroll_parallel_connections"] == 8
    assert opts["external_downloader"]["https"] == "videoroll-aria2c"
    for protocol, size, expected in (
        ("https", 350_000_000, VideoRollAria2cFD),
        ("https", 10_000_000, None),
        ("http_dash_segments", 350_000_000, None),
        ("m3u8_native", 350_000_000, None),
    ):
        info = {"url": "https://example.com/test", "protocol": protocol,
                "ext": "mp4", "filesize": size}
        downloader = get_suitable_downloader(info, opts)
        assert (downloader is VideoRollAria2cFD) == (expected is not None)
    settings.youtube_parallel_download_enabled = False
    assert "external_downloader" not in yd.build_ydl_opts(settings, for_download=True)
    settings.youtube_parallel_download_enabled = True
    settings.youtube_proxy = "socks5://127.0.0.1:1080"
    assert "external_downloader" not in yd.build_ydl_opts(settings, for_download=True)


def test_parse_aria_progress_with_eta():
    parsed = parse_aria2_progress("[#000123 18MiB/222MiB(8%) CN:8 DL:2.4MiB ETA:1m20s]")
    assert parsed is not None
    assert parsed["connections"] == 8
    assert parsed["downloaded_bytes"] == 18 * 1024 * 1024
    assert parsed["eta"] == 80
    assert parsed["speed"] > 2_000_000
    assert parse_aria2_progress("ERR invalid response https://signed-url") is None


def test_aria_failure_uses_separate_native_fallback_and_preserves_partial():
    settings = Settings()
    sentinel = "parallel/f401.mp4.part.aria2"
    expected = (Path("/tmp/native.mp4"), {"id": "demo"}, yd.YouTubeMeta(
        title="demo", description="", webpage_url="https://youtube.com"))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def fake_once(url, settings, *, work_dir, parallel=None, **kwargs):
            if parallel:
                partial = root / sentinel
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"aria2-checkpoint")
                raise DownloadError("aria2 network error")
            assert work_dir == root / "native"
            assert (root / sentinel).read_bytes() == b"aria2-checkpoint"
            return expected
        with patch.object(yd, "_download_once", side_effect=fake_once), patch.object(yd.shutil, "which", return_value="/usr/bin/aria2c"):
            got = yd.download_youtube_video(
                "https://youtube.com/watch?v=demo", settings, work_dir=root)
        assert got == expected
        assert (root / sentinel).exists()


def test_actual_aria2_range_transfer_reports_live_progress():
    if not register_parallel_downloader():
        pytest.skip("aria2c executable unavailable")

    size = 65 * 1024 * 1024
    requests: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass

        def do_GET(self):
            header = self.headers.get("Range")
            requests.append(header)
            start, end = 0, size - 1
            if header:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", header)
                assert match is not None
                start = int(match[1])
                if match[2]:
                    end = min(size - 1, int(match[2]))
            self.send_response(206 if header else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(end - start + 1))
            if header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                for pos in range(start, end + 1, 128 * 1024):
                    length = min(128 * 1024, end + 1 - pos)
                    self.wfile.write(bytes([pos // (1024 * 1024) % 251]) * length)
                    time.sleep(0.021)
            except (ConnectionResetError, BrokenPipeError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    events: list[dict] = []
    try:
        with tempfile.TemporaryDirectory(prefix="videoroll-range-test-") as tmp:
            info = {
                "id": "local-video", "title": "Test",
                "url": f"http://127.0.0.1:{server.server_port}/test.mp4",
                "protocol": "http", "ext": "mp4", "filesize": size,
            }
            with YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
                downloader = VideoRollAria2cFD(
                    ydl, {"continuedl": True, "videoroll_parallel_connections": 8,
                          "quiet": True, "noprogress": True})
                downloader.add_progress_hook(events.append)
                assert downloader.real_download(str(Path(tmp) / "test.mp4"), info)
            assert (Path(tmp) / "test.mp4").stat().st_size == size
            assert len(set(requests)) >= 8  # not just -x8 passed to aria2c
            assert any(
                event.get("status") == "downloading"
                and event.get("download_backend") == "aria2c"
                and event.get("downloaded_bytes", 0) > 0
                and event.get("speed", 0) > 0
                for event in events
            )
    finally:
        server.shutdown()
