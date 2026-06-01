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
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings, update_asr_settings
from videoroll.config import SubtitleServiceSettings
from videoroll.db.models import AppSetting


class _FakeChunk:
    def __init__(self, start_ts: float, end_ts: float, text: str) -> None:
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.text = text


class _FakeResult:
    def __init__(self, chunks: list[_FakeChunk]) -> None:
        self.chunks = chunks


class _FakeGenerationConfig:
    def __init__(self) -> None:
        self.return_timestamps = False
        self.task: str | None = None
        self.language: str | None = None
        self.num_beams = 0
        self.max_new_tokens = 0


class _FakePipeline:
    def __init__(self, result: _FakeResult) -> None:
        self._result = result
        self.cfg = _FakeGenerationConfig()
        self.calls: list[dict[str, object]] = []

    def get_generation_config(self) -> _FakeGenerationConfig:
        return self.cfg

    def generate(self, raw_speech_input: list[float], generation_config: _FakeGenerationConfig | None = None, **kwargs: object) -> _FakeResult:
        self.calls.append(
            {
                "raw_speech_input": raw_speech_input,
                "generation_config": generation_config,
                "kwargs": kwargs,
            }
        )
        return self._result


class _FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, AppSetting] = {}

    def get(self, _model: object, key: str) -> AppSetting | None:
        return self.rows.get(key)

    def add(self, row: AppSetting) -> None:
        self.rows[row.key] = row

    def commit(self) -> None:
        return None

    def refresh(self, row: AppSetting) -> None:
        self.rows[row.key] = row


class OpenVinoAsrTests(unittest.TestCase):
    def _defaults(self, *, database_url: str) -> SubtitleServiceSettings:
        return SubtitleServiceSettings(
            database_url=database_url,
            redis_url="redis://localhost:6379/0",
            s3_endpoint_url="http://localhost:9000",
            s3_access_key_id="key",
            s3_secret_access_key="secret",
            s3_bucket="bucket",
            SUBTITLE_OPENVINO_MODEL="/models/whisper/whisper-large-v3-ov",
            SUBTITLE_OPENVINO_DEVICE="GPU.0",
            SUBTITLE_OPENVINO_NUM_BEAMS=2,
            SUBTITLE_OPENVINO_MAX_NEW_TOKENS=640,
            SUBTITLE_ASR_ENGINE="openvino",
            SUBTITLE_WHISPER_MODEL="tiny",
        )

    def test_asr_settings_store_persists_openvino_defaults(self) -> None:
        defaults = self._defaults(database_url="postgresql+psycopg://videoroll:videoroll@localhost:5432/videoroll")
        db = _FakeDb()

        current = get_asr_settings(db, defaults)
        self.assertEqual(current["default_engine"], "openvino")
        self.assertEqual(current["default_model"], "/models/whisper/whisper-large-v3-ov")
        self.assertEqual(current["openvino_device"], "GPU.0")
        self.assertEqual(current["openvino_num_beams"], 2)
        self.assertEqual(current["openvino_max_new_tokens"], 640)

        updated = update_asr_settings(
            db,
            defaults,
            {
                "default_engine": "openvino",
                "default_model": "/models/whisper/custom-ov",
                "openvino_device": "GPU",
                "openvino_num_beams": 3,
                "openvino_max_new_tokens": 512,
            },
        )
        self.assertEqual(updated["default_model"], "/models/whisper/custom-ov")
        self.assertEqual(updated["openvino_device"], "GPU")
        self.assertEqual(updated["openvino_num_beams"], 3)
        self.assertEqual(updated["openvino_max_new_tokens"], 512)

    def test_transcribe_openvino_whisper_builds_segments_from_chunks(self) -> None:
        fake_pipeline = _FakePipeline(_FakeResult([_FakeChunk(0.25, 1.5, " Hello Intel Arc ")]))

        with (
            patch.object(processing, "_read_wav_as_float_mono_16k", return_value=([0.1, -0.1], 2.0)),
            patch.object(processing, "_get_openvino_pipeline", return_value=fake_pipeline),
        ):
            segments = processing.transcribe_openvino_whisper(
                Path("/tmp/fake.wav"),
                model_name="/models/whisper/whisper-large-v3-ov",
                language="en",
                device="GPU",
                num_beams=2,
                max_new_tokens=256,
            )

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start, 0.25)
        self.assertEqual(segments[0].end, 1.5)
        self.assertEqual(segments[0].text, "Hello Intel Arc")
        self.assertEqual(fake_pipeline.cfg.language, "<|en|>")
        self.assertEqual(fake_pipeline.cfg.task, "transcribe")
        self.assertTrue(fake_pipeline.cfg.return_timestamps)
        self.assertEqual(fake_pipeline.cfg.num_beams, 2)
        self.assertEqual(fake_pipeline.cfg.max_new_tokens, 256)
        self.assertEqual(len(fake_pipeline.calls), 1)


if __name__ == "__main__":
    unittest.main()
