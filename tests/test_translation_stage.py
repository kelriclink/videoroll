from __future__ import annotations

from types import SimpleNamespace

import pytest

from videoroll.apps.subtitle_service import translation_stage
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.translation_stage import (
    TranslationRetryRequired,
    is_retryable_translation_error,
    run_translation_stage,
    translation_retry_countdown,
)


def _settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "default_style": "natural",
        "default_batch_size": 8,
        "default_enable_summary": True,
        "default_max_retries": 2,
        "openai_enable_thinking": False,
        "openai_model": "test-model",
        "openai_temperature": 0.2,
        "openai_timeout_seconds": 30,
    }
    values.update(overrides)
    return values


def test_disabled_translation_does_not_construct_ai_service() -> None:
    segments = [Segment(start=0.0, end=1.0, text="hello")]

    def fail_ai_factory() -> object:
        raise AssertionError("AI service should not be created when translation is disabled")

    result = run_translation_stage(
        db=object(),  # type: ignore[arg-type]
        task_id="task",
        subtitle_job_id="job",
        segments=segments,
        source_segments_key="segments.json",
        translate_cfg={"enabled": False, "target_lang": "zh", "provider": "openai", "bilingual": True},
        checkpoint=object(),  # type: ignore[arg-type]
        trace=object(),  # type: ignore[arg-type]
        retry_attempt=0,
        database_url="postgresql://unused",
        fresh_translate_settings=lambda: _settings(),
        ai_service_factory=fail_ai_factory,  # type: ignore[arg-type]
        log=lambda _message: None,
    )

    assert result.enabled is False
    assert result.segments is segments
    assert result.provider == "openai"
    assert result.target_lang == "zh"
    assert result.bilingual is True


def test_mock_translation_is_owned_by_translation_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    source = [Segment(start=0.0, end=1.0, text="hello")]
    translated = [Segment(start=0.0, end=1.0, text="你好")]
    seen: dict[str, object] = {}

    def fake_translate(segments: list[Segment], *, target_lang: str) -> list[Segment]:
        seen["segments"] = segments
        seen["target_lang"] = target_lang
        return translated

    monkeypatch.setattr(translation_stage, "translate_segments_mock", fake_translate)
    ai_service = object()

    result = run_translation_stage(
        db=object(),  # type: ignore[arg-type]
        task_id="task",
        subtitle_job_id="job",
        segments=source,
        source_segments_key="segments.json",
        translate_cfg={
            "enabled": True,
            "target_lang": "zh",
            "provider": "mock",
            "style": "spoken",
            "bilingual": False,
        },
        checkpoint=object(),  # type: ignore[arg-type]
        trace=object(),  # type: ignore[arg-type]
        retry_attempt=0,
        database_url="postgresql://unused",
        fresh_translate_settings=lambda: _settings(),
        ai_service_factory=lambda: ai_service,  # type: ignore[arg-type]
        log=lambda _message: None,
    )

    assert seen == {"segments": source, "target_lang": "zh"}
    assert result.enabled is True
    assert result.segments == translated
    assert result.style == "spoken"
    assert result.ai_service is ai_service


def test_openai_failure_requests_celery_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDB:
        def rollback(self) -> None:
            pass

    class FakeCheckpoint:
        def load(self, _segments: list[Segment], *, source_segments_key: str | None):
            assert source_segments_key == "segments.json"
            return [], ""

    class FakeTrace:
        def __init__(self) -> None:
            self.finished: list[dict[str, object]] = []

        def start_session(self, **_kwargs: object) -> str:
            return "session-1"

        def finish_session(self, _run_id: str | None, **kwargs: object) -> None:
            self.finished.append(dict(kwargs))

    fake_trace = FakeTrace()
    monkeypatch.setattr(
        translation_stage,
        "rag_settings_from_translate_settings",
        lambda _settings: SimpleNamespace(enabled=False),
    )

    def fail_translate(*_args: object, **_kwargs: object):
        raise RuntimeError("temporary provider outage")

    monkeypatch.setattr(translation_stage, "translate_segments_openai_with_summary", fail_translate)

    with pytest.raises(TranslationRetryRequired) as raised:
        run_translation_stage(
            db=FakeDB(),  # type: ignore[arg-type]
            task_id="task",
            subtitle_job_id="job",
            segments=[Segment(start=0.0, end=1.0, text="hello")],
            source_segments_key="segments.json",
            translate_cfg={"enabled": True, "target_lang": "zh", "provider": "openai"},
            checkpoint=FakeCheckpoint(),  # type: ignore[arg-type]
            trace=fake_trace,  # type: ignore[arg-type]
            retry_attempt=0,
            database_url="postgresql://unused",
            fresh_translate_settings=lambda: _settings(),
            ai_service_factory=lambda: object(),  # type: ignore[arg-type]
            log=lambda _message: None,
        )

    retry = raised.value
    assert retry.retry_no == 1
    assert retry.max_retries == 2
    assert retry.countdown == 2.0
    assert str(retry.cause) == "temporary provider outage"
    assert fake_trace.finished[-1]["status"] == "failed"


def test_translation_retry_policy_rejects_missing_api_key() -> None:
    assert is_retryable_translation_error(RuntimeError("API key is not set")) is False
    assert is_retryable_translation_error(RuntimeError("timeout")) is True
    assert translation_retry_countdown(1) == 2.0
    assert translation_retry_countdown(10) == 30.0
