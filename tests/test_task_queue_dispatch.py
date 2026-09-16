from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service import worker
from videoroll.apps.subtitle_service.worker import (
    _mark_queued_job_dispatched,
    _queued_job_dispatch_due,
    celery_app,
)
from videoroll.apps.subtitle_service.queues import SUBTITLE_CONTROL_QUEUE
from videoroll.db.base import Base
from videoroll.db.models import RenderJob, SourceLicense, SourceType, SubtitleJob, Task


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def test_subtitle_worker_prefetches_only_one_long_task_per_process() -> None:
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_periodic_control_tasks_use_the_control_queue() -> None:
    schedules = dict(celery_app.conf.beat_schedule)
    assert schedules
    assert {item["options"]["queue"] for item in schedules.values()} == {SUBTITLE_CONTROL_QUEUE}


def test_queued_job_is_not_redispatched_immediately() -> None:
    now = datetime.now(timezone.utc)
    job = SubtitleJob(progress=0)
    job.updated_at = now

    _mark_queued_job_dispatched(job)

    assert job.progress == 1
    assert _queued_job_dispatch_due(job, now + timedelta(seconds=30)) is False


def test_queued_job_is_retried_after_dispatch_timeout() -> None:
    now = datetime.now(timezone.utc)
    job = SubtitleJob(progress=1)
    job.updated_at = now - timedelta(seconds=61)

    assert _queued_job_dispatch_due(job, now) is True


@pytest.mark.parametrize("job_type", [SubtitleJob, RenderJob])
def test_each_persisted_retry_waits_another_full_dispatch_interval(job_type, monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, job_type.__table__])
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(worker, "_now", lambda: now)
    monkeypatch.setattr("videoroll.realtime.publish_ui_event", lambda *args, **kwargs: True)

    with Session(engine, autoflush=False) as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = job_type(task_id=task.id, progress=1, request_json={}, updated_at=now - timedelta(seconds=61))
        db.add(job)
        db.commit()
        assert _queued_job_dispatch_due(job, now)

        _mark_queued_job_dispatched(job)
        db.commit()
        db.refresh(job)

        assert not _queued_job_dispatch_due(job, now + timedelta(seconds=59))
        assert _queued_job_dispatch_due(job, now + timedelta(seconds=60))
    engine.dispose()
