from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.subtitle_service.main import _read_task_queue
from videoroll.apps.subtitle_service.worker import TASK_QUEUE_LOCK_OWNER
from videoroll.db.base import Base
from videoroll.db.models import (
    AppSetting,
    RenderJob,
    RenderJobStatus,
    SourceLicense,
    SourceType,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    tables = [Task.__table__, AppSetting.__table__, SubtitleJob.__table__, RenderJob.__table__]
    Base.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine, tables=list(reversed(tables)))


def _task(status: TaskStatus) -> Task:
    return Task(source_type=SourceType.local, source_license=SourceLicense.own, status=status)


def test_task_queue_does_not_show_stopped_tasks_or_count_their_locks(db: Session) -> None:
    now = datetime.now(timezone.utc)
    stopped_subtitle = _task(TaskStatus.canceled)
    stopped_subtitle.stopped_status = TaskStatus.downloaded
    stopped_subtitle.lock_owner = TASK_QUEUE_LOCK_OWNER
    stopped_subtitle.lock_until = now + timedelta(minutes=5)
    stopped_render = _task(TaskStatus.canceled)
    stopped_render.stopped_status = TaskStatus.subtitle_ready
    db.add_all([stopped_subtitle, stopped_render])
    db.flush()
    db.add(
        SubtitleJob(
            task_id=stopped_subtitle.id,
            status=SubtitleJobStatus.queued,
            request_json={},
        )
    )
    db.add(
        RenderJob(
            task_id=stopped_render.id,
            status=RenderJobStatus.running,
            request_json={},
        )
    )
    db.commit()

    queue = _read_task_queue(db, limit=200)

    assert queue.running_count == 0
    assert queue.queued_count == 0
    assert queue.tasks == []


def test_task_queue_does_not_show_published_tasks_or_count_their_jobs(db: Session) -> None:
    now = datetime.now(timezone.utc)
    published = _task(TaskStatus.published)
    published.lock_owner = TASK_QUEUE_LOCK_OWNER
    published.lock_until = now + timedelta(minutes=5)
    db.add(published)
    db.flush()
    db.add(RenderJob(task_id=published.id, status=RenderJobStatus.queued, request_json={}))
    db.commit()

    queue = _read_task_queue(db, limit=200)

    assert queue.running_count == 0
    assert queue.queued_count == 0
    assert queue.tasks == []


def test_task_queue_shows_live_leased_job_as_running_after_task_lock_expires(db: Session) -> None:
    now = datetime.now(timezone.utc)
    task = _task(TaskStatus.subtitle_ready)
    db.add(task)
    db.flush()
    job = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.running,
        progress=42,
        request_json={},
        lease_owner="worker-a",
        lease_until=now + timedelta(minutes=5),
    )
    db.add(job)
    db.commit()

    queue = _read_task_queue(db, limit=200)

    assert queue.running_count == 1
    assert queue.queued_count == 0
    assert len(queue.tasks) == 1
    assert queue.tasks[0].state == "running"
    assert queue.tasks[0].stage == "render"
    assert queue.tasks[0].render_job_id == job.id
