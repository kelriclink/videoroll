from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from videoroll.utils.auto_youtube import encode_auto_youtube_created_by

from videoroll.apps.subtitle_service import worker


def test_worker_has_no_rss_memory_limit() -> None:
    assert worker.celery_app.conf.worker_max_memory_per_child is None
    assert worker.celery_app.conf.worker_max_tasks_per_child == 20


def test_runtime_profile_marker_preserves_manual_override_on_automatic_task() -> None:
    task = SimpleNamespace(created_by=encode_auto_youtube_created_by("auto_youtube", auto_publish=None))

    assert worker._uses_runtime_auto_profile(task, {}) is True
    assert worker._uses_runtime_auto_profile(task, {"runtime_profile": True}) is True
    assert worker._uses_runtime_auto_profile(task, {"runtime_profile": False}) is False
    assert worker._uses_runtime_auto_profile(task, {}, {"runtime_profile": False}) is False


def test_active_pipeline_job_prefers_existing_subtitle_and_avoids_render_lookup() -> None:
    task_id = uuid.uuid4()
    subtitle_id = uuid.uuid4()
    subtitle = SimpleNamespace(id=subtitle_id)
    subtitle_query = Mock()
    subtitle_query.filter.return_value.order_by.return_value.first.return_value = subtitle
    db = Mock()
    db.query.return_value = subtitle_query

    assert worker._active_pipeline_job(db, task_id) == ("subtitle", subtitle_id)
    db.query.assert_called_once_with(worker.SubtitleJob)


def test_active_pipeline_job_reuses_render_when_no_subtitle_is_active() -> None:
    task_id = uuid.uuid4()
    render_id = uuid.uuid4()
    subtitle_query = Mock()
    subtitle_query.filter.return_value.order_by.return_value.first.return_value = None
    render_query = Mock()
    render_query.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(id=render_id)
    db = Mock()
    db.query.side_effect = [subtitle_query, render_query]

    assert worker._active_pipeline_job(db, task_id) == ("render", render_id)


@pytest.mark.parametrize("override,expected", [(0, 8), (20, 8), (2, 2)])
def test_cpu_threads_share_the_task_concurrency_budget(monkeypatch: pytest.MonkeyPatch, override: int, expected: int) -> None:
    monkeypatch.setattr(worker, "get_task_queue_settings", lambda _db: {"max_concurrency": 2})
    monkeypatch.setattr(worker.settings, "whisper_cpu_threads", override)
    monkeypatch.setattr(worker.settings, "whisper_num_workers", 1)
    monkeypatch.setattr(worker, "process_cpu_count", lambda: 16)

    assert worker._asr_cpu_threads(object()) == expected
