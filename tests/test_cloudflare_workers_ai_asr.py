from __future__ import annotations

import wave
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from videoroll.apps.subtitle_service import processing
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings, update_asr_settings


def _defaults() -> SimpleNamespace:
    return SimpleNamespace(
        asr_engine="cloudflare-workers-ai",
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
        cloudflare_workers_ai_account_id="account-from-env",
        cloudflare_workers_ai_api_key="key-from-env",
        cloudflare_workers_ai_model="@cf/openai/whisper-large-v3-turbo",
    )


def test_cloudflare_settings_expose_defaults() -> None:
    db = MagicMock()
    db.get.return_value = None

    settings = get_asr_settings(db, _defaults())

    assert settings["default_model"] == "@cf/openai/whisper-large-v3-turbo"
    assert settings["cloudflare_workers_ai_account_id"] == "account-from-env"
    assert settings["cloudflare_workers_ai_model"] == "@cf/openai/whisper-large-v3-turbo"
    assert settings["cloudflare_workers_ai_api_key"] == "key-from-env"
    assert settings["cloudflare_workers_ai_api_key_set"] is True


def test_cloudflare_settings_persist_encrypted_values() -> None:
    db = MagicMock()
    rows = {}
    db.get.side_effect = lambda _model, key: rows.get(key)
    db.add.side_effect = lambda row: rows.__setitem__(row.key, row)
    db.refresh.side_effect = lambda row: rows.__setitem__(row.key, row)

    updated = update_asr_settings(
        db,
        _defaults(),
        {
            "default_engine": "cloudflare-workers-ai",
            "cloudflare_workers_ai_account_id": "account-from-db",
            "cloudflare_workers_ai_model": "@cf/openai/whisper-large-v3-turbo",
            "cloudflare_workers_ai_api_key": "secret-token",
        },
    )

    assert updated["cloudflare_workers_ai_account_id"] == "account-from-db"
    assert updated["cloudflare_workers_ai_api_key"] == "secret-token"
    assert rows["subtitle.asr"].value_json["cloudflare_workers_ai"]["api_key_enc"] != "secret-token"


def test_cloudflare_transcription_parses_segments_and_native_request(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")
    response = MagicMock()
    response.json.return_value = {
        "result": {
            "text": "Hello world",
            "segments": [{"start": 0.25, "end": 1.5, "text": " Hello world "}],
        },
        "success": True,
    }
    response.raise_for_status.return_value = None

    with patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response) as post:
        segments = processing.transcribe_cloudflare_workers_ai(
            audio_path,
            account_id="account-id",
            api_key="secret",
            model_name="@cf/openai/whisper-large-v3-turbo",
            language="zh",
        )

    assert segments == [processing.Segment(start=0.25, end=1.5, text="Hello world")]
    assert post.call_args.args[0] == (
        "https://api.cloudflare.com/client/v4/accounts/account-id/ai/run/"
        "@cf/openai/whisper-large-v3-turbo"
    )
    assert post.call_args.kwargs["headers"] == {
        "Authorization": "Bearer secret",
        "Content-Type": "application/json",
    }
    body = post.call_args.kwargs["json"]
    assert body["task"] == "transcribe"
    assert body["language"] == "zh"
    assert body["audio"]


def test_cloudflare_transcription_uses_word_timestamps_when_segments_missing(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")
    response = MagicMock()
    response.json.return_value = {
        "result": {
            "text": "Hello world",
            "words": [
                {"start": 0.4, "end": 0.8, "word": " Hello "},
                {"start": 0.9, "end": 1.3, "word": " world "},
            ],
        }
    }
    response.raise_for_status.return_value = None

    with patch("videoroll.apps.subtitle_service.processing.httpx.post", return_value=response):
        segments = processing.transcribe_cloudflare_workers_ai(
            audio_path,
            account_id="account-id",
            api_key="secret",
        )

    assert segments == [processing.Segment(start=0.4, end=1.3, text="Hello world")]


def test_cloudflare_transcription_chunks_wav_and_offsets_timestamps(tmp_path) -> None:
    audio_path = tmp_path / "long.wav"
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(100)
        wav_file.writeframes(b"\x00\x00" * 3100)

    first = MagicMock()
    first.json.return_value = {"result": {"segments": [{"start": 1.0, "end": 2.0, "text": "first"}]}}
    first.raise_for_status.return_value = None
    second = MagicMock()
    second.json.return_value = {"result": {"segments": [{"start": 0.2, "end": 0.8, "text": "second"}]}}
    second.raise_for_status.return_value = None

    with patch(
        "videoroll.apps.subtitle_service.processing.httpx.post",
        side_effect=[first, second],
    ) as post:
        segments = processing.transcribe_cloudflare_workers_ai(
            audio_path,
            account_id="account-id",
            api_key="secret",
        )

    assert post.call_count == 2
    assert segments == [
        processing.Segment(start=1.0, end=2.0, text="first"),
        processing.Segment(start=30.2, end=30.8, text="second"),
    ]


def test_cloudflare_transcription_rejects_invalid_account_id(tmp_path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")

    try:
        processing.transcribe_cloudflare_workers_ai(
            audio_path,
            account_id="account/id",
            api_key="secret",
        )
    except RuntimeError as exc:
        assert "invalid characters" in str(exc)
    else:
        raise AssertionError("invalid account ID should fail")
