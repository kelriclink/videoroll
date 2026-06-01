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

from videoroll.apps.subtitle_service import processing


class ProcessingAsrSilenceTests(unittest.TestCase):
    def test_audio_silence_probe_distinguishes_real_signal(self) -> None:
        self.assertTrue(processing._audio_is_effectively_silent([0.0] * 128))
        self.assertTrue(processing._audio_is_effectively_silent([0.0005, -0.0005] * 128))
        self.assertFalse(processing._audio_is_effectively_silent([0.0, 0.03] * 128))

    def test_transcribe_faster_whisper_skips_model_for_effectively_silent_audio(self) -> None:
        with patch.object(processing, "_read_wav_as_float_mono_16k", return_value=([0.0] * 320, 2.0)):
            segments = processing.transcribe_faster_whisper(
                Path("/tmp/silent.wav"),
                model_name="tiny",
            )

        self.assertEqual(segments, [])

    def test_transcribe_openvino_whisper_skips_pipeline_for_effectively_silent_audio(self) -> None:
        with (
            patch.object(processing, "_read_wav_as_float_mono_16k", return_value=([0.0] * 320, 2.0)),
            patch.object(processing, "_get_openvino_pipeline", side_effect=AssertionError("pipeline should not be created")),
        ):
            segments = processing.transcribe_openvino_whisper(
                Path("/tmp/silent.wav"),
                model_name="/models/whisper/whisper-large-v3-ov",
                language="auto",
                device="GPU",
                num_beams=2,
                max_new_tokens=256,
            )

        self.assertEqual(segments, [])


if __name__ == "__main__":
    unittest.main()
