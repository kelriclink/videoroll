from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from videoroll.apps.subtitle_service import processing
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings


def _defaults() -> SimpleNamespace:
    return SimpleNamespace(
        asr_engine="external-whisper",
        whisper_model="tiny",
        openvino_model="",
        openvino_device="GPU",
        openvino_num_beams=1,
        openvino_max_new_tokens=448,
        openvino_vad_enabled=True,
        openvino_vad_threshold=0.5,
        external_whisper_base_url="https://api.example/v1",
        external_whisper_api_key="env-key",
        external_whisper_model="whisper-1",
    )


def test_asr_settings_exposes_external_whisper_defaults() -> None:
    db = MagicMock()
    db.get.return_value = None

    settings = get_asr_settings(db, _defaults())

    assert settings["default_model"] == "whisper-1"
    assert settings["external_whisper_base_url"] == "https://api.example/v1"
    assert settings["external_whisper_model"] == "whisper-1"
    assert settings["external_whisper_api_key"] == "env-key"
    assert settings["external_whisper_api_key_set"] is True


def test_external_whisper_parses_verbose_segments(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")
    response = MagicMock()
    response.json.return_value = {
        "text": "Hello world",
        "segments": [{"start": 0.25, "end": 1.5, "text": " Hello world "}],
    }
    response.raise_for_status.return_value = None

    with patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response) as post:
        segments = processing.transcribe_external_whisper(
            audio_path,
            base_url="https://api.example/v1",
            api_key="secret",
            model_name="whisper-1",
            language="en",
        )

    assert segments[0].start == 0.25
    assert segments[0].end == 1.5
    assert segments[0].text == "Hello world"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert post.call_args.kwargs["data"] == {"model": "whisper-1", "response_format": "verbose_json", "language": "en"}
    assert post.call_args.args[0] == "https://api.example/v1/audio/transcriptions"


def test_external_whisper_falls_back_to_one_segment_for_plain_text_response(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")
    response = MagicMock()
    response.json.return_value = {"text": "Hello"}
    response.raise_for_status.return_value = None

    with (
        patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response),
        patch.object(processing, "_read_wav_as_float_mono_16k", return_value=([0.0], 2.0)),
    ):
        segments = processing.transcribe_external_whisper(
            audio_path,
            base_url="https://api.example/v1/",
            api_key="secret",
            model_name="custom-whisper",
        )

    assert segments == [processing.Segment(start=0.0, end=2.0, text="Hello")]
