from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from videoroll.apps.orchestrator_api import main
from videoroll.db.models import SourceLicense, SourceType, Task, TaskStatus


class _FakeQuery:
    def __init__(self, *, all_result=None, count_result: int | None = None):
        self._all_result = all_result
        self._count_result = count_result

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._all_result

    def count(self):
        if self._count_result is None:
            raise AssertionError("count_result was not configured")
        return self._count_result


def _failed_task(*, source_type: SourceType, source_url: str | None = None) -> Task:
    return Task(
        id=uuid.uuid4(),
        source_type=source_type,
        source_url=source_url,
        source_license=SourceLicense.own,
        status=TaskStatus.failed,
    )


def test_resume_failed_recent_restarts_youtube_pipeline_when_no_subtitle_job(monkeypatch) -> None:
    task = _failed_task(source_type=SourceType.youtube, source_url="https://www.youtube.com/watch?v=abc123")
    db = MagicMock()
    db.query.side_effect = [
        _FakeQuery(all_result=[task]),
        _FakeQuery(count_result=0),
        _FakeQuery(count_result=0),
    ]

    set_created_by_calls: list[tuple[uuid.UUID, str]] = []

    monkeypatch.setattr(main, "_build_auto_publish_after_render", lambda *args, **kwargs: {"publish": True})
    monkeypatch.setattr(
        main,
        "_build_resume_subtitle_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPException(status_code=400, detail="no subtitle job found to resume")
        ),
    )
    monkeypatch.setattr(main, "get_auto_profile", lambda _db: {"auto_publish": True})
    monkeypatch.setattr(
        main,
        "_set_task_created_by",
        lambda _settings, *, task_id, created_by: set_created_by_calls.append((task_id, created_by)),
    )
    monkeypatch.setattr(main, "_enqueue_auto_youtube_pipeline", lambda task_id, auto_publish: f"pipeline:{task_id}:{auto_publish}")

    resp = main.resume_recent_failed_tasks(
        window_hours=24,
        limit=200,
        settings=SimpleNamespace(),
        db=db,
        s3=MagicMock(),
    )

    assert resp.resumed_count == 1
    assert resp.skipped_count == 0
    assert resp.failed_count == 0
    assert resp.results[0].task_id == task.id
    assert resp.results[0].status == "queued"
    assert "started auto_youtube pipeline" in str(resp.results[0].detail)
    assert set_created_by_calls and set_created_by_calls[0][0] == task.id


def test_resume_failed_recent_skips_non_youtube_task_without_subtitle_job(monkeypatch) -> None:
    task = _failed_task(source_type=SourceType.local, source_url=None)
    db = MagicMock()
    db.query.side_effect = [
        _FakeQuery(all_result=[task]),
        _FakeQuery(count_result=0),
        _FakeQuery(count_result=0),
    ]

    monkeypatch.setattr(main, "_build_auto_publish_after_render", lambda *args, **kwargs: {"publish": True})
    monkeypatch.setattr(
        main,
        "_build_resume_subtitle_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPException(status_code=400, detail="no subtitle job found to resume")
        ),
    )

    resp = main.resume_recent_failed_tasks(
        window_hours=24,
        limit=200,
        settings=SimpleNamespace(),
        db=db,
        s3=MagicMock(),
    )

    assert resp.resumed_count == 0
    assert resp.skipped_count == 1
    assert resp.failed_count == 0
    assert resp.results[0].task_id == task.id
    assert resp.results[0].status == "skipped"
    assert resp.results[0].detail == "no subtitle job found to resume"
