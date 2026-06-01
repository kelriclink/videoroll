from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import httpx as _httpx  # type: ignore
except ModuleNotFoundError:
    fake_httpx = types.ModuleType("httpx")

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    fake_httpx.Client = Client
    sys.modules["httpx"] = fake_httpx

from videoroll.apps.subtitle_service.processing import render_burn_in


class ProcessingRenderTests(unittest.TestCase):
    def test_render_burn_in_cpu_h264_uses_libx264(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged):
            render_burn_in(
                "ffmpeg",
                Path("/tmp/input.mp4"),
                Path("/tmp/subtitle.ass"),
                Path("/tmp/out.mp4"),
                video_codec="h264",
                preset="fast",
                crf=20,
            )

        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("libx264", cmd)
        self.assertIn("fast", cmd)
        self.assertIn("20", cmd)
        self.assertIn("ass=/tmp/subtitle.ass", cmd)
        self.assertNotIn("h264_vaapi", cmd)

    def test_render_burn_in_intel_h264_uses_vaapi(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
        ):
            render_burn_in(
                "ffmpeg",
                Path("/tmp/input.mp4"),
                Path("/tmp/subtitle.ass"),
                Path("/tmp/out.mp4"),
                video_codec="h264",
                use_intel_gpu=True,
                intel_gpu_render_device="/dev/dri/renderD128",
                preset="slow",
                crf=21,
            )

        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("/dev/dri/renderD128", cmd)
        self.assertIn("h264_vaapi", cmd)
        self.assertIn("CQP", cmd)
        self.assertIn("21", cmd)
        self.assertIn("3", cmd)
        self.assertIn("ass=/tmp/subtitle.ass,format=nv12,hwupload", cmd)

    def test_render_burn_in_intel_av1_uses_vaapi(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
        ):
            render_burn_in(
                "ffmpeg",
                Path("/tmp/input.mp4"),
                Path("/tmp/subtitle.ass"),
                Path("/tmp/out.mp4"),
                video_codec="av1",
                use_intel_gpu=True,
                intel_gpu_render_device="/dev/dri/renderD128",
                preset="4",
                crf=26,
            )

        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("/dev/dri/renderD128", cmd)
        self.assertIn("av1_vaapi", cmd)
        self.assertIn("CQP", cmd)
        self.assertIn("26", cmd)
        self.assertIn("3", cmd)
        self.assertIn("ass=/tmp/subtitle.ass,format=nv12,hwupload", cmd)

    def test_render_burn_in_intel_av1_requires_ffmpeg_encoder(self) -> None:
        with (
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "av1_vaapi"):
                render_burn_in(
                    "ffmpeg",
                    Path("/tmp/input.mp4"),
                    Path("/tmp/subtitle.ass"),
                    Path("/tmp/out.mp4"),
                    video_codec="av1",
                    use_intel_gpu=True,
                    intel_gpu_render_device="/dev/dri/renderD128",
                )


if __name__ == "__main__":
    unittest.main()
