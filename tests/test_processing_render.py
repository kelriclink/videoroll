from __future__ import annotations

import subprocess
import sys
import tempfile
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

from videoroll.apps.subtitle_service.processing import (
    Segment,
    _prepare_ass_overlay_band,
    probe_video_bit_depth,
    render_burn_in,
    segments_to_ass,
)


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

    def test_render_burn_in_intel_av1_uses_transparent_vaapi_overlay_band(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_filter", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=8),
            patch("videoroll.apps.subtitle_service.processing.probe_video_resolution", return_value=(3840, 2160)),
            patch("videoroll.apps.subtitle_service.processing.probe_video_frame_rate", return_value="60/1"),
            patch(
                "videoroll.apps.subtitle_service.processing._prepare_ass_overlay_band",
                return_value=(Path("/tmp/subtitle-band.ass"), 760),
            ),
            patch("videoroll.apps.subtitle_service.processing.Path.unlink"),
        ):
            render_burn_in(
                "ffmpeg",
                Path("/tmp/input.webm"),
                Path("/tmp/subtitle.ass"),
                Path("/tmp/out.mp4"),
                video_codec="av1",
                use_intel_gpu=True,
                intel_gpu_render_device="/dev/dri/renderD128",
                crf=26,
            )

        self.assertEqual(len(calls), 2)
        cmd = calls[-1]
        self.assertIn("-filter_complex", cmd)
        self.assertIn("color=c=black@0.0:s=3840x760:r=60/1,format=yuva420p", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("ass=/tmp/subtitle-band.ass:alpha=1,format=bgra,hwupload", graph)
        self.assertIn("overlay_vaapi=x=0:y=1400:shortest=1", graph)
        self.assertNotIn("hwdownload", graph)
        self.assertIn("[out]", cmd)
        self.assertIn("0:a?", cmd)

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

    def test_prepare_ass_overlay_band_keeps_font_scale_and_reduces_height(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "subtitle.ass"
            source.write_text(
                segments_to_ass(
                    [
                        Segment(
                            start=0.0,
                            end=4.0,
                            text="这是第一行中文字幕，保持当前大字号。",
                            secondary_text="This is the smaller English subtitle line.",
                        )
                    ],
                    play_res_x=3840,
                    play_res_y=2160,
                    secondary_line_scale=0.68,
                    primary_font_scale_percent=127,
                    secondary_font_scale_percent=90,
                ),
                encoding="utf-8",
            )

            prepared = _prepare_ass_overlay_band(
                source,
                video_width=3840,
                video_height=2160,
                output_dir=root,
            )

            self.assertIsNotNone(prepared)
            assert prepared is not None
            band_path, band_height = prepared
            band_text = band_path.read_text(encoding="utf-8")
            self.assertGreaterEqual(band_height, 192)
            self.assertLess(band_height, 2160)
            self.assertIn(f"PlayResY: {band_height}", band_text)
            self.assertIn("Style: Default,Noto Sans CJK SC,149", band_text)

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
