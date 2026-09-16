from __future__ import annotations

import subprocess
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

from videoroll.apps.subtitle_service.processing import probe_video_bit_depth, render_burn_in


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
        self.assertNotIn("-filter_threads", cmd)
        self.assertNotIn("-threads:v", cmd)

    def test_render_burn_in_intel_h264_uses_vaapi(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=10),
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

        self.assertEqual(len(calls), 2)
        preflight, cmd = calls
        self.assertIn("scale_vaapi=format=nv12,hwdownload,format=nv12", preflight)
        self.assertEqual(cmd[cmd.index("-init_hw_device") + 1], "vaapi=va:/dev/dri/renderD128")
        self.assertEqual(cmd[cmd.index("-filter_hw_device") + 1], "va")
        self.assertIn("-hwaccel", cmd)
        self.assertIn("-hwaccel_output_format", cmd)
        self.assertIn("h264_vaapi", cmd)
        self.assertIn("CQP", cmd)
        self.assertIn("21", cmd)
        self.assertIn("3", cmd)
        self.assertIn(
            "scale_vaapi=format=nv12,hwdownload,format=nv12,ass=/tmp/subtitle.ass,format=nv12,hwupload",
            cmd,
        )
        self.assertNotIn("-filter_threads", cmd)
        self.assertNotIn("-threads:v", cmd)

    def test_render_burn_in_intel_av1_uses_vaapi(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=8),
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

        self.assertEqual(len(calls), 2)
        cmd = calls[-1]
        self.assertEqual(cmd[cmd.index("-init_hw_device") + 1], "vaapi=va:/dev/dri/renderD128")
        self.assertIn("-hwaccel", cmd)
        self.assertIn("av1_vaapi", cmd)
        self.assertIn("CQP", cmd)
        self.assertIn("26", cmd)
        self.assertIn("3", cmd)
        self.assertIn(
            "scale_vaapi=format=nv12,hwdownload,format=nv12,ass=/tmp/subtitle.ass,format=nv12,hwupload",
            cmd,
        )
        self.assertNotIn("-filter_threads", cmd)
        self.assertNotIn("-threads:v", cmd)

    def test_render_burn_in_intel_av1_preserves_10bit_input(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=10),
        ):
            render_burn_in(
                "ffmpeg",
                Path("/tmp/input.webm"),
                Path("/tmp/subtitle.ass"),
                Path("/tmp/out.mp4"),
                video_codec="av1",
                use_intel_gpu=True,
                intel_gpu_render_device="/dev/dri/renderD128",
            )

        self.assertEqual(len(calls), 2)
        preflight, cmd = calls
        self.assertIn("scale_vaapi=format=p010,hwdownload,format=p010le", preflight)
        self.assertIn(
            "scale_vaapi=format=p010,hwdownload,format=p010le,ass=/tmp/subtitle.ass,format=p010le,hwupload",
            cmd,
        )

    def test_render_burn_in_falls_back_to_software_decode_and_keeps_vaapi_encode(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=10),
        ):
            render_burn_in(
                "ffmpeg",
                Path("/tmp/input.webm"),
                Path("/tmp/subtitle.ass"),
                Path("/tmp/out.mp4"),
                video_codec="av1",
                use_intel_gpu=True,
                intel_gpu_render_device="/dev/dri/renderD128",
            )

        self.assertEqual(len(calls), 2)
        cmd = calls[-1]
        self.assertNotIn("-hwaccel", cmd)
        self.assertNotIn("-hwaccel_output_format", cmd)
        self.assertIn("ass=/tmp/subtitle.ass,format=p010le,hwupload", cmd)
        self.assertIn("av1_vaapi", cmd)

    def test_probe_video_bit_depth_reads_ffprobe_pixel_format(self) -> None:
        result = types.SimpleNamespace(
            stdout='{"streams":[{"pix_fmt":"yuv420p10le","bits_per_raw_sample":"0"}]}'
        )
        with patch("videoroll.apps.subtitle_service.processing.subprocess.run", return_value=result):
            depth = probe_video_bit_depth("ffmpeg", Path("/tmp/input.webm"))

        self.assertEqual(depth, 10)

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
