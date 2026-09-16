from __future__ import annotations

import wave
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from videoroll.apps.subtitle_service import processing
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings


def _defaults() -> SimpleNamespace:
    return SimpleNamespace(
        asr_engine="groq-whisper",
        whisper_model="tiny",
        openvino_model="",
        openvino_device="GPU",
        openvino_num_beams=1,
        openvino_max_new_tokens=448,
        openvino_vad_enabled=True,
        openvino_vad_threshold=0.5,
        external_whisper_base_url="",
        external_whisper_api_key=None,
        external_whisper_model="whisper-1",
        groq_whisper_api_key="env-groq-key",
        groq_whisper_model="whisper-large-v3-turbo",
    )


def _wav(path, seconds: int = 2) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00" * 16000 * seconds)


def test_groq_settings_exposes_dedicated_defaults() -> None:
    db = MagicMock()
    db.get.return_value = None

    settings = get_asr_settings(db, _defaults())

    assert settings["default_engine"] == "groq-whisper"
    assert settings["default_model"] == "whisper-large-v3-turbo"
    assert settings["groq_whisper_model"] == "whisper-large-v3-turbo"
    assert settings["groq_whisper_api_key"] == "env-groq-key"
    assert settings["groq_whisper_api_key_set"] is True


def test_groq_settings_ignores_legacy_local_default_model() -> None:
    db = MagicMock()
    db.get.return_value = MagicMock(value_json={"default_engine": "groq-whisper", "default_model": "tiny"})

    settings = get_asr_settings(db, _defaults())

    assert settings["default_model"] == "whisper-large-v3-turbo"


def test_groq_default_chunk_policy_is_gateway_safe() -> None:
    assert processing._GROQ_CHUNK_SECONDS == 45.0
    assert processing._GROQ_CHUNK_OVERLAP_SECONDS == 5.0
    assert processing._GROQ_MAX_REQUEST_ATTEMPTS == 5
    assert processing._GROQ_RETRY_BASE_SECONDS == 3.0


def test_groq_whisper_posts_verbose_json_and_preserves_segments(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    _wav(audio_path)
    response = MagicMock()
    response.json.return_value = {
        "text": "Hello world",
        "segments": [{"start": 0.25, "end": 1.5, "text": " Hello world "}],
    }
    response.raise_for_status.return_value = None

    with (
        patch("videoroll.apps.subtitle_service.processing._encode_groq_flac_chunk", return_value=b"flac") as encode,
        patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response) as post,
    ):
        segments = processing.transcribe_groq_whisper(
            audio_path,
            api_key="gsk-test",
            model_name="whisper-large-v3-turbo",
            vad_enabled=False,
        )

    assert segments == [processing.Segment(start=0.25, end=1.5, text="Hello world")]
    assert post.call_args.args[0] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer gsk-test"}
    assert post.call_args.kwargs["files"] == {"file": ("audio-part-1.flac", b"flac", "audio/flac")}
    assert post.call_args.kwargs["data"] == {
        "model": "whisper-large-v3-turbo",
        "response_format": "verbose_json",
        "temperature": "0",
    }
    encode.assert_called_once()


def test_groq_whisper_chunks_large_wav_and_offsets_results(tmp_path) -> None:
    audio_path = tmp_path / "long.wav"
    _wav(audio_path, seconds=100)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "segments": [{"start": 0.5, "end": 1.5, "text": "hello"}],
    }

    with (
        patch("videoroll.apps.subtitle_service.processing._encode_groq_flac_chunk", return_value=b"flac"),
        patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response) as post,
    ):
        segments = processing.transcribe_groq_whisper(
            audio_path,
            api_key="gsk-test",
            model_name="whisper-large-v3",
            vad_enabled=False,
        )

    assert post.call_count == 3
    assert len(segments) == 3
    assert segments[0].start == 0.5
    # The default 5-second overlap moves the second chunk start back from
    # the first chunk's end.
    assert segments[1].start == 40.5
    assert segments[2].start == 80.5


def test_groq_overlap_stitches_continuing_sentence_before_translation() -> None:
    segments = processing._merge_groq_segments(
        [
            (
                0.0,
                [processing.Segment(start=39.0, end=44.0, text="The best way of making it square")],
            ),
            (
                40.0,
                [
                    processing.Segment(
                        start=0.5,
                        end=6.0,
                        text="making it square is to calculate the diagonal length",
                    )
                ],
            ),
        ]
    )

    assert segments == [
        processing.Segment(
            start=39.0,
            end=46.0,
            text="The best way of making it square is to calculate the diagonal length",
        )
    ]


def test_groq_overlap_uses_more_complete_near_duplicate() -> None:
    segments = processing._merge_groq_segments(
        [
            (0.0, [processing.Segment(start=40.0, end=44.0, text="then we install the panel")]),
            (
                40.0,
                [
                    processing.Segment(
                        start=0.2,
                        end=5.3,
                        text="and then we install the panels here",
                    )
                ],
            ),
        ]
    )

    assert segments == [
        processing.Segment(
            start=40.0,
            end=45.3,
            text="and then we install the panels here",
        )
    ]


def test_groq_overlap_reconciles_one_segment_against_multiple_previous_segments() -> None:
    segments = processing._merge_groq_segments(
        [
            (
                0.0,
                [
                    processing.Segment(start=39.0, end=41.0, text="the best way"),
                    processing.Segment(start=41.0, end=44.0, text="is to calculate the diagonal"),
                ],
            ),
            (
                40.0,
                [
                    processing.Segment(
                        start=0.2,
                        end=5.5,
                        text="the best way is to calculate the diagonal length",
                    )
                ],
            ),
        ]
    )

    assert segments == [
        processing.Segment(
            start=39.0,
            end=45.5,
            text="the best way is to calculate the diagonal length",
        )
    ]


def test_asr_overlap_clips_unrelated_text_instead_of_dropping_it() -> None:
    segments = processing.reconcile_overlapping_asr_segments(
        [
            processing.Segment(start=10.0, end=12.0, text="first unrelated sentence"),
            processing.Segment(start=11.8, end=13.0, text="second different caption"),
        ]
    )

    assert segments == [
        processing.Segment(start=10.0, end=11.8, text="first unrelated sentence"),
        processing.Segment(start=11.8, end=13.0, text="second different caption"),
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Turn the valve on", "Turn the valve off"),
        ("The answer is yes", "The answer is no"),
        ("yes", "yes"),
    ],
)
def test_asr_edge_overlap_preserves_each_spoken_caption(first: str, second: str) -> None:
    segments = processing.reconcile_overlapping_asr_segments(
        [
            processing.Segment(start=10.0, end=12.0, text=first),
            processing.Segment(start=11.99, end=14.0, text=second),
        ]
    )

    assert segments == [
        processing.Segment(start=10.0, end=11.99, text=first),
        processing.Segment(start=11.99, end=14.0, text=second),
    ]


def test_asr_adjacent_repetitions_remain_separate() -> None:
    segments = [
        processing.Segment(start=10.0, end=12.0, text="yes"),
        processing.Segment(start=12.0, end=14.0, text="yes"),
    ]

    assert processing.reconcile_overlapping_asr_segments(segments) == segments


@pytest.mark.parametrize("duration", [0.20, 0.08])
def test_groq_full_overlap_deduplicates_short_captions(duration: float) -> None:
    segments = processing._merge_groq_segments(
        [
            (0.0, [processing.Segment(start=40.0, end=40.0 + duration, text="yes")]),
            (40.0, [processing.Segment(start=0.0, end=duration, text="yes")]),
        ]
    )

    assert segments == [processing.Segment(start=40.0, end=40.0 + duration, text="yes")]


def test_asr_substantial_unrelated_overlap_preserves_both_texts_in_one_caption() -> None:
    segments = processing.reconcile_overlapping_asr_segments(
        [
            processing.Segment(start=10.0, end=15.0, text="main speaker continues talking"),
            processing.Segment(start=11.0, end=12.0, text="short audience response"),
        ]
    )

    assert segments == [
        processing.Segment(
            start=10.0,
            end=15.0,
            text="main speaker continues talking short audience response",
        )
    ]


def test_asr_repeated_text_without_time_overlap_remains_separate() -> None:
    segments = processing.reconcile_overlapping_asr_segments(
        [
            processing.Segment(start=1.0, end=2.0, text="yes"),
            processing.Segment(start=2.5, end=3.0, text="yes"),
        ]
    )

    assert len(segments) == 2


def test_groq_whisper_retries_transient_disconnect(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    _wav(audio_path)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "segments": [{"start": 0.0, "end": 1.0, "text": "recovered"}],
    }

    with (
        patch("videoroll.apps.subtitle_service.processing._encode_groq_flac_chunk", return_value=b"flac"),
        patch(
            "videoroll.apps.subtitle_service.processing.httpx.post",
            side_effect=[processing.httpx.RemoteProtocolError("server disconnected"), response],
        ) as post,
        patch("videoroll.apps.subtitle_service.processing.time.sleep") as sleep,
    ):
        segments = processing.transcribe_groq_whisper(audio_path, api_key="gsk-test", vad_enabled=False)

    assert segments == [processing.Segment(start=0.0, end=1.0, text="recovered")]
    assert post.call_count == 2
    sleep.assert_called_once()


def test_groq_whisper_resumes_after_last_successful_chunk(tmp_path) -> None:
    audio_path = tmp_path / "long.wav"
    checkpoint_path = tmp_path / "groq-checkpoint.json"
    _wav(audio_path, seconds=100)

    first_response = MagicMock()
    first_response.raise_for_status.return_value = None
    first_response.json.return_value = {
        "segments": [{"start": 0.0, "end": 1.0, "text": "first"}],
    }
    failure = processing.httpx.RemoteProtocolError("server disconnected")

    with (
        patch("videoroll.apps.subtitle_service.processing._encode_groq_flac_chunk", return_value=b"flac"),
        patch(
            "videoroll.apps.subtitle_service.processing.httpx.post",
            side_effect=[first_response] + [failure] * processing._GROQ_MAX_REQUEST_ATTEMPTS,
        ) as post,
        patch("videoroll.apps.subtitle_service.processing.time.sleep"),
    ):
        try:
            processing.transcribe_groq_whisper(
                audio_path,
                api_key="gsk-test",
                checkpoint_path=checkpoint_path,
                audio_identity="audio-key",
                vad_enabled=False,
            )
        except RuntimeError as exc:
            assert "chunk 2/3" in str(exc)
        else:
            raise AssertionError("expected second chunk to fail")

    assert post.call_count == 1 + processing._GROQ_MAX_REQUEST_ATTEMPTS
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert list(payload["completed_chunks"]) == ["1"]

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "segments": [{"start": 0.0, "end": 1.0, "text": "remaining"}],
    }
    with (
        patch("videoroll.apps.subtitle_service.processing._encode_groq_flac_chunk", return_value=b"flac") as encode,
        patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response) as post,
    ):
        segments = processing.transcribe_groq_whisper(
            audio_path,
            api_key="gsk-test",
            checkpoint_path=checkpoint_path,
            audio_identity="audio-key",
            vad_enabled=False,
        )

    assert post.call_count == 2
    assert encode.call_count == 2
    assert [segment.text for segment in segments] == ["first", "remaining", "remaining"]


def test_groq_whisper_vad_skips_silent_chunk_and_saves_empty_checkpoint(tmp_path) -> None:
    audio_path = tmp_path / "silent.wav"
    checkpoint_path = tmp_path / "groq-checkpoint.json"
    _wav(audio_path)

    with (
        patch("videoroll.apps.subtitle_service.processing._groq_window_has_speech", return_value=False) as vad,
        patch(
            "videoroll.apps.subtitle_service.processing._encode_groq_flac_chunk",
            side_effect=AssertionError("silent chunk must not be encoded"),
        ),
        patch(
            "videoroll.apps.subtitle_service.processing.httpx.post",
            side_effect=AssertionError("silent chunk must not be uploaded"),
        ),
    ):
        segments = processing.transcribe_groq_whisper(
            audio_path,
            api_key="gsk-test",
            checkpoint_path=checkpoint_path,
            audio_identity="silent-audio",
            vad_enabled=True,
            vad_threshold=0.6,
        )

    assert segments == []
    vad.assert_called_once_with(audio_path, offset=0.0, duration=2.0, threshold=0.6)
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert payload["vad_enabled"] is True
    assert payload["vad_threshold"] == 0.6
    assert payload["completed_chunks"]["1"]["segments"] == []


def test_groq_window_vad_short_circuits_effective_silence_without_model(tmp_path) -> None:
    audio_path = tmp_path / "silent.wav"
    _wav(audio_path)

    with patch(
        "videoroll.apps.subtitle_service.processing._detect_silero_speech_spans",
        side_effect=AssertionError("effective silence should not load the VAD model"),
    ):
        has_speech = processing._groq_window_has_speech(
            audio_path,
            offset=0.0,
            duration=2.0,
            threshold=0.5,
        )

    assert has_speech is False


def test_groq_whisper_restores_vad_skipped_chunk_without_rechecking(tmp_path) -> None:
    audio_path = tmp_path / "silent.wav"
    checkpoint_path = tmp_path / "groq-checkpoint.json"
    _wav(audio_path)

    with patch("videoroll.apps.subtitle_service.processing._groq_window_has_speech", return_value=False):
        processing.transcribe_groq_whisper(
            audio_path,
            api_key="gsk-test",
            checkpoint_path=checkpoint_path,
            audio_identity="silent-audio",
            vad_enabled=True,
        )

    with (
        patch(
            "videoroll.apps.subtitle_service.processing._groq_window_has_speech",
            side_effect=AssertionError("completed empty chunk must come from checkpoint"),
        ),
        patch(
            "videoroll.apps.subtitle_service.processing.httpx.post",
            side_effect=AssertionError("completed empty chunk must not be uploaded"),
        ),
    ):
        segments = processing.transcribe_groq_whisper(
            audio_path,
            api_key="gsk-test",
            checkpoint_path=checkpoint_path,
            audio_identity="silent-audio",
            vad_enabled=True,
        )

    assert segments == []
