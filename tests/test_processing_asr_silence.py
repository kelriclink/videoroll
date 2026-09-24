from __future__ import annotations

import sys
import tempfile
import types
import unittest
import wave
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

from videoroll.apps.subtitle_service import processing


class ProcessingAsrSilenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audio_path = Path(directory.name) / "silent.wav"
        with wave.open(str(self.audio_path), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            target.writeframes(b"\x00\x00" * 32000)

    def test_audio_silence_probe_distinguishes_real_signal(self) -> None:
        self.assertTrue(processing._audio_is_effectively_silent([0.0] * 128))
        self.assertTrue(processing._audio_is_effectively_silent([0.0005, -0.0005] * 128))
        self.assertFalse(processing._audio_is_effectively_silent([0.0, 0.03] * 128))

    def test_transcribe_faster_whisper_skips_model_for_effectively_silent_audio(self) -> None:
        segments = processing.transcribe_faster_whisper(self.audio_path, model_name="tiny")

        self.assertEqual(segments, [])

    def test_transcribe_openvino_whisper_skips_pipeline_for_effectively_silent_audio(self) -> None:
        with (
            patch.object(processing, "_get_openvino_pipeline", side_effect=AssertionError("pipeline should not be created")),
        ):
            segments = processing.transcribe_openvino_whisper(
                self.audio_path,
                model_name="/models/whisper/whisper-large-v3-ov",
                language="auto",
                device="GPU",
                num_beams=2,
                max_new_tokens=256,
            )

        self.assertEqual(segments, [])

    def test_timing_refiner_snaps_caption_to_detected_speech(self) -> None:
        segment = processing.Segment(start=1.0, end=3.0, text="hello world")
        speech = processing._OpenVinoSpeechSpan(
            start_sample=int(0.55 * 16000),
            end_sample=int(2.10 * 16000),
        )

        with (
            patch.object(processing, "_read_wav_window_as_float_mono_16k", return_value=[0.0] * 16) as read_window,
            patch.object(processing, "_detect_silero_speech_spans", return_value=[speech]) as detect,
        ):
            refined = processing.refine_asr_segment_timing(self.audio_path, [segment])

        self.assertEqual(len(refined), 1)
        self.assertAlmostEqual(refined[0].start, 1.20, places=2)
        self.assertAlmostEqual(refined[0].end, 2.75, places=2)
        self.assertEqual(refined[0].text, segment.text)
        read_window.assert_called_once()
        self.assertAlmostEqual(read_window.call_args.kwargs["offset"], 0.65, places=2)
        self.assertAlmostEqual(read_window.call_args.kwargs["duration"], 2.70, places=2)
        self.assertEqual(detect.call_args.kwargs["min_silence_duration_ms"], 160)
        self.assertEqual(detect.call_args.kwargs["speech_pad_ms"], 80)

    def test_timing_refiner_falls_back_when_silero_is_unavailable(self) -> None:
        segment = processing.Segment(start=1.0, end=3.0, text="keep original")

        with (
            patch.object(processing, "_read_wav_window_as_float_mono_16k", return_value=[0.0] * 16),
            patch.object(processing, "_detect_silero_speech_spans", return_value=None),
        ):
            refined = processing.refine_asr_segment_timing(self.audio_path, [segment])

        self.assertEqual(refined, [segment])

    def test_timing_refiner_resolves_vad_expansion_overlap_without_merging_text(self) -> None:
        first = processing.Segment(start=1.0, end=2.0, text="first")
        second = processing.Segment(start=2.1, end=3.1, text="second")
        speech_spans = [
            [
                processing._OpenVinoSpeechSpan(
                    start_sample=int(0.45 * 16000),
                    end_sample=int(1.65 * 16000),
                )
            ],
            [
                processing._OpenVinoSpeechSpan(
                    start_sample=int(0.15 * 16000),
                    end_sample=int(1.25 * 16000),
                )
            ],
        ]

        with (
            patch.object(processing, "_read_wav_window_as_float_mono_16k", return_value=[0.0] * 16),
            patch.object(processing, "_detect_silero_speech_spans", side_effect=speech_spans),
        ):
            refined = processing.refine_asr_segment_timing(
                self.audio_path,
                [first, second],
            )

        self.assertEqual([item.text for item in refined], ["first", "second"])
        self.assertAlmostEqual(refined[0].end, refined[1].start, places=6)
        self.assertGreaterEqual(refined[0].end - refined[0].start, 0.12)
        self.assertGreaterEqual(refined[1].end - refined[1].start, 0.12)


if __name__ == "__main__":
    unittest.main()
