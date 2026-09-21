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
    _ass_can_use_reduced_overlay_rate,
    _prepare_ass_overlay_band,
    _reduced_overlay_frame_rate,
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
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_filter", return_value=False),
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
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_filter", return_value=False),
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
            patch("videoroll.apps.subtitle_service.processing._ass_can_use_reduced_overlay_rate", return_value=True),
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
        self.assertIn("color=c=black@0.0:s=3840x760:r=15/1,format=yuva420p", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("ass=/tmp/subtitle-band.ass:alpha=1,format=bgra,hwupload", graph)
        self.assertIn("overlay_vaapi=x=0:y=1400:shortest=1", graph)
        self.assertNotIn("hwdownload", graph)
        self.assertIn("[out]", cmd)
        self.assertIn("0:a?", cmd)

    def test_render_burn_in_intel_av1_10bit_uses_p010_vaapi_overlay(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_filter", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=10),
            patch("videoroll.apps.subtitle_service.processing.probe_video_resolution", return_value=(3840, 2160)),
            patch("videoroll.apps.subtitle_service.processing.probe_video_frame_rate", return_value="60/1"),
            patch(
                "videoroll.apps.subtitle_service.processing._prepare_ass_overlay_band",
                return_value=(Path("/tmp/subtitle-band.ass"), 1008),
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
            )

        self.assertEqual(len(calls), 2)
        preflight, cmd = calls
        self.assertIn("scale_vaapi=format=p010,hwdownload,format=p010le", preflight)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:v]scale_vaapi=format=p010[main]", graph)
        self.assertIn("ass=/tmp/subtitle-band.ass:alpha=1,format=bgra,hwupload", graph)
        self.assertIn("overlay_vaapi=x=0:y=1152:shortest=1", graph)
        self.assertNotIn("hwdownload", graph)


    def test_render_burn_in_software_decode_still_uses_gpu_subtitle_overlay(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_filter", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=10),
            patch("videoroll.apps.subtitle_service.processing.probe_video_resolution", return_value=(3840, 2160)),
            patch("videoroll.apps.subtitle_service.processing.probe_video_frame_rate", return_value="60/1"),
            patch(
                "videoroll.apps.subtitle_service.processing._prepare_ass_overlay_band",
                return_value=(Path("/tmp/subtitle-band.ass"), 1008),
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
            )

        self.assertEqual(len(calls), 2)
        cmd = calls[-1]
        self.assertNotIn("-hwaccel", cmd)
        self.assertNotIn("-hwaccel_output_format", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:v]format=p010le,hwupload[main]", graph)
        self.assertIn("overlay_vaapi", graph)
        self.assertIn("av1_vaapi", cmd)


    def test_render_burn_in_complex_ass_uses_full_frame_gpu_overlay(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_filter", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=8),
            patch("videoroll.apps.subtitle_service.processing.probe_video_resolution", return_value=(1920, 1080)),
            patch("videoroll.apps.subtitle_service.processing.probe_video_frame_rate", return_value="30/1"),
            patch("videoroll.apps.subtitle_service.processing._prepare_ass_overlay_band", return_value=None),
        ):
            render_burn_in(
                "ffmpeg",
                Path("/tmp/input.webm"),
                Path("/tmp/subtitle.ass"),
                Path("/tmp/out.mp4"),
                video_codec="av1",
                use_intel_gpu=True,
            )

        self.assertEqual(len(calls), 2)
        cmd = calls[-1]
        self.assertIn("color=c=black@0.0:s=1920x1080:r=30/1,format=yuva420p", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("ass=/tmp/subtitle.ass:alpha=1,format=bgra,hwupload", graph)
        self.assertIn("overlay_vaapi=x=0:y=0:shortest=1", graph)
        self.assertNotIn("hwdownload", graph)

    def test_render_burn_in_overlay_runtime_failure_retries_legacy_path(self) -> None:
        calls: list[list[str]] = []

        def fake_run_logged(cmd: list[str], **_kwargs: object) -> None:
            calls.append(cmd)
            if len(calls) == 2:
                raise subprocess.CalledProcessError(1, cmd)

        with (
            patch("videoroll.apps.subtitle_service.processing._run_logged", side_effect=fake_run_logged),
            patch("videoroll.apps.subtitle_service.processing.Path.exists", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_encoder", return_value=True),
            patch("videoroll.apps.subtitle_service.processing._ffmpeg_supports_filter", return_value=True),
            patch("videoroll.apps.subtitle_service.processing.probe_video_bit_depth", return_value=10),
            patch("videoroll.apps.subtitle_service.processing.probe_video_resolution", return_value=(3840, 2160)),
            patch("videoroll.apps.subtitle_service.processing.probe_video_frame_rate", return_value="60/1"),
            patch(
                "videoroll.apps.subtitle_service.processing._prepare_ass_overlay_band",
                return_value=(Path("/tmp/subtitle-band.ass"), 1008),
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
            )

        self.assertEqual(len(calls), 3)
        overlay_cmd = calls[1]
        legacy_cmd = calls[2]
        self.assertIn("-filter_complex", overlay_cmd)
        self.assertIn(
            "scale_vaapi=format=p010,hwdownload,format=p010le,ass=/tmp/subtitle.ass,format=p010le,hwupload",
            legacy_cmd,
        )

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

    def test_prepare_ass_overlay_band_does_not_reserve_quarter_of_4k_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "subtitle.ass"
            source.write_text(
                """[Script Info]
PlayResX: 3840
PlayResY: 2160
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,96,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,4,0,2,160,160,80,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:04.00,Default,,0,0,0,,普通单行字幕
""",
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
            _band_path, band_height = prepared
            self.assertGreaterEqual(band_height, 192)
            self.assertLess(band_height, int(2160 * 0.24))

    def test_static_ass_can_use_reduced_overlay_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "subtitle.ass"
            source.write_text(
                """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.20,0:00:05.96,Default,,0,0,0,,{\\rDefault}中文\\N{\\rSecondary}English
""",
                encoding="utf-8",
            )
            self.assertTrue(_ass_can_use_reduced_overlay_rate(source))
            self.assertEqual(_reduced_overlay_frame_rate("60/1"), "15/1")
            self.assertEqual(_reduced_overlay_frame_rate("30000/1001"), "15/1")
            self.assertEqual(_reduced_overlay_frame_rate("12/1"), "12/1")

    def test_animated_ass_keeps_source_overlay_rate(self) -> None:
        animated_texts = [
            r"{\t(0,500,\alpha&H80&)}Animated",
            r"{\fad(200,200)}Fade",
            r"{\k20}Ka{\kf30}raoke",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, text in enumerate(animated_texts):
                source = root / f"animated-{index}.ass"
                source.write_text(
                    "[Events]\n"
                    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                    f"Dialogue: 0,0:00:00.00,0:00:04.00,Default,,0,0,0,,{text}\n",
                    encoding="utf-8",
                )
                self.assertFalse(_ass_can_use_reduced_overlay_rate(source), text)

            effect = root / "effect.ass"
            effect.write_text(
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:04.00,Default,,0,0,0,Banner,Scrolling\n",
                encoding="utf-8",
            )
            self.assertFalse(_ass_can_use_reduced_overlay_rate(effect))

    def test_render_burn_in_animated_ass_keeps_video_frame_rate(self) -> None:
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
            patch("videoroll.apps.subtitle_service.processing._ass_can_use_reduced_overlay_rate", return_value=False),
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
            )

        cmd = calls[-1]
        self.assertIn("color=c=black@0.0:s=3840x760:r=60/1,format=yuva420p", cmd)

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
