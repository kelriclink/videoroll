from __future__ import annotations

import pytest

from videoroll.apps.subtitle_service import worker


def test_worker_has_no_rss_memory_limit() -> None:
    assert worker.celery_app.conf.worker_max_memory_per_child is None
    assert worker.celery_app.conf.worker_max_tasks_per_child == 20


@pytest.mark.parametrize("override,expected", [(0, 8), (20, 8), (2, 2)])
def test_cpu_threads_share_the_task_concurrency_budget(monkeypatch: pytest.MonkeyPatch, override: int, expected: int) -> None:
    monkeypatch.setattr(worker, "get_task_queue_settings", lambda _db: {"max_concurrency": 2})
    monkeypatch.setattr(worker.settings, "whisper_cpu_threads", override)
    monkeypatch.setattr(worker.settings, "whisper_num_workers", 1)
    monkeypatch.setattr(worker, "process_cpu_count", lambda: 16)

    assert worker._asr_cpu_threads(object()) == expected
