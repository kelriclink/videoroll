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
        external_whisper_min_silence_ms=2000,
        external_whisper_speech_pad_ms=400,
        external_whisper_condition_on_previous_text=True,
        external_whisper_max_segment_seconds=12.0,
        external_whisper_max_segment_chars=120,
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
    assert settings["external_whisper_min_silence_ms"] == 2000
    assert settings["external_whisper_speech_pad_ms"] == 400
    assert settings["external_whisper_condition_on_previous_text"] is True
    assert settings["external_whisper_max_segment_seconds"] == 12.0
    assert settings["external_whisper_max_segment_chars"] == 120


def test_asr_settings_upgrades_exact_legacy_external_profile() -> None:
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        value_json={
            "external_whisper": {
                "base_url": "http://whisper.internal:8000/v1",
                "model": "whisper-large-v3",
                "min_silence_ms": 500,
                "speech_pad_ms": 180,
                "condition_on_previous_text": False,
                "max_segment_seconds": 6.0,
                "max_segment_chars": 80,
            }
        }
    )

    settings = get_asr_settings(db, _defaults())

    assert settings["external_whisper_min_silence_ms"] == 2000
    assert settings["external_whisper_speech_pad_ms"] == 400
    assert settings["external_whisper_condition_on_previous_text"] is True
    assert settings["external_whisper_max_segment_seconds"] == 12.0
    assert settings["external_whisper_max_segment_chars"] == 120


def test_asr_settings_keeps_custom_external_profile() -> None:
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        value_json={
            "external_whisper": {
                "min_silence_ms": 650,
                "speech_pad_ms": 220,
                "condition_on_previous_text": False,
                "max_segment_seconds": 9.0,
                "max_segment_chars": 96,
            }
        }
    )

    settings = get_asr_settings(db, _defaults())

    assert settings["external_whisper_min_silence_ms"] == 650
    assert settings["external_whisper_speech_pad_ms"] == 220
    assert settings["external_whisper_condition_on_previous_text"] is False
    assert settings["external_whisper_max_segment_seconds"] == 9.0
    assert settings["external_whisper_max_segment_chars"] == 96

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
    assert sent["condition_on_previous_text"] == "true"
    assert sent["min_silence_duration_ms"] == "2000"
    assert sent["speech_pad_ms"] == "400"
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


def test_external_whisper_regroups_sentence_across_provider_segments() -> None:
    payload = {
        "duration": 4.0,
        "segments": [
            {
                "start": 0.0,
                "end": 1.8,
                "text": "This is not",
                "words": [
                    {"word": " This", "start": 0.0, "end": 0.4, "probability": 0.99},
                    {"word": " is", "start": 0.4, "end": 0.8, "probability": 0.99},
                    {"word": " not", "start": 0.8, "end": 1.2, "probability": 0.99},
                ],
            },
            {
                "start": 1.2,
                "end": 3.4,
                "text": "two different sentences.",
                "words": [
                    {"word": " two", "start": 1.2, "end": 1.6, "probability": 0.99},
                    {"word": " different", "start": 1.6, "end": 2.3, "probability": 0.99},
                    {"word": " sentences.", "start": 2.3, "end": 3.4, "probability": 0.99},
                ],
            },
        ],
    }

    segments = processing._normalize_external_whisper_payload(
        payload, audio_duration=4.0, max_segment_seconds=12.0, max_segment_chars=120
    )

    assert [(segment.start, segment.end, segment.text) for segment in segments] == [
        (0.0, 3.4, "This is not two different sentences.")
    ]


def test_external_whisper_uses_speech_gap_as_semantic_boundary() -> None:
    payload = {
        "duration": 5.0,
        "segments": [
            {
                "start": 0.0,
                "end": 5.0,
                "text": "First thought next thought",
                "words": [
                    {"word": " First", "start": 0.0, "end": 0.5},
                    {"word": " thought", "start": 0.5, "end": 1.2},
                    {"word": " next", "start": 2.1, "end": 2.6},
                    {"word": " thought", "start": 2.6, "end": 3.3},
                ],
            }
        ],
    }

    segments = processing._normalize_external_whisper_payload(
        payload, audio_duration=5.0, max_segment_seconds=12.0, max_segment_chars=120
    )

    assert [segment.text for segment in segments] == ["First thought", "next thought"]


def test_external_whisper_respects_cjk_terminal_punctuation() -> None:
    payload = {
        "duration": 4.0,
        "segments": [
            {
                "start": 0.0,
                "end": 4.0,
                "text": "这是第一句。这是第二句。",
                "words": [
                    {"word": "这是", "start": 0.0, "end": 0.6},
                    {"word": "第一句。", "start": 0.6, "end": 1.6},
                    {"word": "这是", "start": 1.7, "end": 2.3},
                    {"word": "第二句。", "start": 2.3, "end": 3.4},
                ],
            }
        ],
    }

    segments = processing._normalize_external_whisper_payload(
        payload, audio_duration=4.0, max_segment_seconds=12.0, max_segment_chars=120
    )

    assert [segment.text for segment in segments] == ["这是第一句。", "这是第二句。"]


def test_external_whisper_preserves_reasonable_provider_segment_without_words() -> None:
    payload = {
        "duration": 10.0,
        "segments": [
            {
                "start": 0.0,
                "end": 10.0,
                "text": "This provider sentence lasts longer than six seconds but should stay intact.",
            }
        ],
    }

    segments = processing._normalize_external_whisper_payload(
        payload, audio_duration=10.0, max_segment_seconds=6.0, max_segment_chars=80
    )

    assert segments == [
        processing.Segment(
            start=0.0,
            end=10.0,
            text="This provider sentence lasts longer than six seconds but should stay intact.",
        )
    ]


def test_semantic_regroup_prefers_comma_boundary_under_soft_pressure() -> None:
    words = []
    for index in range(16):
        token = f" word{index}"
        if index == 7:
            token += ","
        words.append(
            processing._ASRWord(
                start=index * 0.4,
                end=(index + 1) * 0.4,
                text=token,
                probability=0.99,
            )
        )

    segments = processing._semantic_regroup_asr_words(
        words, max_segment_seconds=12.0, max_segment_chars=200
    )

    assert len(segments) == 2
    assert segments[0].text.endswith(",")
    assert segments[0].end == pytest.approx(3.2)
    assert segments[1].start == pytest.approx(3.2)


def test_semantic_regroup_uses_soft_speech_gap_before_hard_limit() -> None:
    words = [
        processing._ASRWord(0.0, 0.5, " First"),
        processing._ASRWord(0.5, 1.0, " idea"),
        processing._ASRWord(1.0, 1.5, " keeps"),
        processing._ASRWord(1.5, 2.0, " going"),
        processing._ASRWord(2.5, 3.0, " second"),
        processing._ASRWord(3.0, 3.5, " idea"),
        processing._ASRWord(3.5, 4.0, " keeps"),
        processing._ASRWord(4.0, 4.5, " going"),
        processing._ASRWord(4.5, 5.0, " further"),
        processing._ASRWord(5.0, 5.5, " today"),
        processing._ASRWord(5.5, 6.0, " for"),
        processing._ASRWord(6.0, 6.5, " testing"),
        processing._ASRWord(6.5, 7.0, " this"),
        processing._ASRWord(7.0, 7.5, " boundary"),
        processing._ASRWord(7.5, 8.0, " scoring"),
        processing._ASRWord(8.0, 8.5, " logic"),
    ]

    segments = processing._semantic_regroup_asr_words(
        words, max_segment_seconds=12.0, max_segment_chars=200
    )

    assert len(segments) == 2
    assert segments[0].text == "First idea keeps going"
    assert segments[0].end == pytest.approx(2.0)
    assert segments[1].text.startswith("second idea")


def test_tiny_fragment_merge_prefers_following_clause() -> None:
    tiny = [processing._ASRWord(1.0, 1.3, " So")]
    following = [
        processing._ASRWord(1.4, 1.8, " we"),
        processing._ASRWord(1.8, 2.3, " continue"),
    ]

    merged = processing._merge_tiny_asr_word_chunks(
        [tiny, following], hard_seconds=12.0, max_chars=120
    )

    assert len(merged) == 1
    assert processing._join_asr_word_text(merged[0]) == "So we continue"

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
