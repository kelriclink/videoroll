"""Regression coverage for automatic no-speech source publishing."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from videoroll.apps.subtitle_service.no_speech import (
    ensure_no_speech_final_asset,
    latest_no_speech_job,
)
from videoroll.db.models import AssetKind, SubtitleJobStatus, TaskStatus


def _job_query(job):
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = job
    return db


def test_no_speech_marker_is_durable_without_audio_read():
    tid = uuid.uuid4()
    db = _job_query(SimpleNamespace(
        status=SubtitleJobStatus.succeeded,
        request_json={"artifacts": {"no_speech_detected": True}},
    ))
    assert latest_no_speech_job(db, tid) is not None


def test_legacy_empty_segments_are_verified_and_nonempty_are_rejected(tmp_path):
    tid = uuid.uuid4()
    artifact = tmp_path / "segments.json"
    job = SimpleNamespace(
        status=SubtitleJobStatus.succeeded,
        request_json={"artifacts": {"source_segments_key": "segments.json"}},
    )
    db = _job_query(job)
    store = MagicMock()
    store.path_for.return_value = artifact

    artifact.write_text("[]", encoding="utf-8")
    assert latest_no_speech_job(db, tid, store=store) is job
    artifact.write_text(json.dumps([{"start": 0, "end": 1, "text": "Hi"}]), encoding="utf-8")
    assert latest_no_speech_job(db, tid, store=store) is None
    job.status = SubtitleJobStatus.failed
    artifact.write_text("[]", encoding="utf-8")
    assert latest_no_speech_job(db, tid, store=store) is None


def test_no_speech_final_media_alias_uses_existing_raw_without_fake_subtitles():
    tid = uuid.uuid4()
    source = SimpleNamespace(storage_key="raw/task/video.webm", sha256="abc", size_bytes=1234, duration_ms=7200)
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.side_effect = [None, source]
    task = SimpleNamespace(id=tid, status=TaskStatus.asr_done,
                           error_code="NO_SPEECH_DETECTED", error_message="old failure")
    result = ensure_no_speech_final_asset(db, task)
    assert result.kind == AssetKind.video_final
    assert result.storage_key == source.storage_key
    assert result.size_bytes == source.size_bytes
    assert task.status == TaskStatus.rendered
    assert task.error_code is None
    assert task.error_message is None
    assert any(getattr(call.args[0], "kind", None) == AssetKind.video_final for call in db.add.call_args_list)


def test_no_speech_does_not_override_published_or_canceled_tasks():
    for status in (TaskStatus.canceled, TaskStatus.publishing, TaskStatus.published):
        db = MagicMock()
        with pytest.raises(ValueError):
            ensure_no_speech_final_asset(db, SimpleNamespace(id=uuid.uuid4(), status=status))
        db.add.assert_not_called()
