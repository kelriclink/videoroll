from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import wave

import pytest

from videoroll.apps.subtitle_service import processing
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings
from videoroll.apps.subtitle_service.schemas import ExternalWhisperTestRequest


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
        external_whisper_batch_size=1,
        external_whisper_vad_enabled=True,
        external_whisper_vad_threshold=0.5,
        external_whisper_min_silence_ms=500,
        external_whisper_speech_pad_ms=180,
        external_whisper_condition_on_previous_text=False,
        external_whisper_max_segment_seconds=6.0,
        external_whisper_max_segment_chars=80,
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
    assert settings["external_whisper_batch_size"] == 1
    assert settings["external_whisper_vad_enabled"] is True
    assert settings["external_whisper_vad_threshold"] == 0.5
    assert settings["external_whisper_min_silence_ms"] == 500
    assert settings["external_whisper_speech_pad_ms"] == 180
    assert settings["external_whisper_condition_on_previous_text"] is False
    assert settings["external_whisper_max_segment_seconds"] == 6.0
    assert settings["external_whisper_max_segment_chars"] == 80


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
    sent = post.call_args.kwargs["data"]
    assert sent["model"] == "whisper-1"
    assert sent["response_format"] == "verbose_json"
    assert sent["language"] == "en"
    assert sent["batch_size"] == "1"
    assert sent["word_timestamps"] == "true"
    assert sent["vad_filter"] == "true"
    assert sent["condition_on_previous_text"] == "false"
    assert sent["min_silence_duration_ms"] == "500"
    assert sent["speech_pad_ms"] == "180"
    assert post.call_args.args[0] == "https://api.example/v1/audio/transcriptions"


def test_external_whisper_falls_back_to_one_segment_for_plain_text_response(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    with wave.open(str(audio_path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\x00\x00" * 32000)
    response = MagicMock()
    response.json.return_value = {"text": "Hello"}
    response.raise_for_status.return_value = None

    with (
        patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response),
    ):
        segments = processing.transcribe_external_whisper(
            audio_path,
            base_url="https://api.example/v1/",
            api_key="secret",
            model_name="custom-whisper",
        )

    assert segments == [processing.Segment(start=0.0, end=2.0, text="Hello")]


def test_external_whisper_allows_local_service_without_api_key(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")
    response = MagicMock()
    response.json.return_value = {
        "text": "Local faster whisper",
        "segments": [{"start": 0.0, "end": 1.0, "text": "Local faster whisper"}],
    }
    response.raise_for_status.return_value = None

    with patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response) as post:
        segments = processing.transcribe_external_whisper(
            audio_path,
            base_url="http://192.168.5.50:8000",
            api_key="",
            model_name="",
        )

    assert segments[0].text == "Local faster whisper"
    assert post.call_args.kwargs["headers"] == {}
    assert post.call_args.kwargs["data"]["model"] == "whisper-1"
    assert post.call_args.args[0] == "http://192.168.5.50:8000/v1/audio/transcriptions"


def test_external_whisper_accepts_full_transcriptions_endpoint(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")
    response = MagicMock()
    response.json.return_value = {"text": ""}
    response.raise_for_status.return_value = None

    endpoint = "http://whisper.internal:8000/v1/audio/transcriptions"
    with patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response) as post:
        processing.transcribe_external_whisper(
            audio_path,
            base_url=endpoint,
            api_key="",
            model_name="whisper-1",
        )

    assert post.call_args.args[0] == endpoint


def test_external_whisper_test_request_only_requires_service_address() -> None:
    request = ExternalWhisperTestRequest(base_url="http://whisper.internal:8000")

    assert request.api_key is None
    assert request.model == "whisper-1"


def test_external_whisper_splits_oversized_segment_using_word_timestamps(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")
    words = [
        {"word": f" word{i}", "start": float(i), "end": float(i + 1), "probability": 0.99}
        for i in range(18)
    ]
    response = MagicMock()
    response.json.return_value = {
        "duration": 18.0,
        "text": "".join(word["word"] for word in words).strip(),
        "segments": [
            {
                "start": 0.0,
                "end": 18.0,
                "text": "".join(word["word"] for word in words),
                "words": words,
            }
        ],
    }
    response.raise_for_status.return_value = None

    with patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response):
        segments = processing.transcribe_external_whisper(
            audio_path,
            base_url="http://whisper.internal:8000/v1",
            api_key="",
            model_name="whisper-1",
            max_segment_seconds=6.0,
            max_segment_chars=200,
        )

    assert len(segments) == 3
    assert all(segment.end - segment.start <= 6.0 for segment in segments)
    assert [segment.start for segment in segments] == [0.0, 6.0, 12.0]
    assert [segment.end for segment in segments] == [6.0, 12.0, 18.0]


def test_external_whisper_plain_text_without_timestamps_is_rejected_for_long_audio(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    with wave.open(str(audio_path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\x00\x00" * (16000 * 18))
    response = MagicMock()
    response.json.return_value = {
        "text": "First sentence. Second sentence. Third sentence. Fourth sentence."
    }
    response.raise_for_status.return_value = None

    with (
        patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response),
        pytest.raises(RuntimeError, match="without segment/word timestamps"),
    ):
        processing.transcribe_external_whisper(
            audio_path,
            base_url="http://whisper.internal:8000/v1",
            api_key="",
            model_name="whisper-1",
            max_segment_seconds=6.0,
            max_segment_chars=40,
        )
