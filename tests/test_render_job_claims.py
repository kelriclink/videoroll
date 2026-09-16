from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.subtitle_service.worker import _cancel_unclaimable_render_job
from videoroll.db.base import Base
from videoroll.db.models import RenderJob, RenderJobStatus, SourceLicense, SourceType, Task, TaskStatus


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, RenderJob.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine, tables=[RenderJob.__table__, Task.__table__])


def _task(db: Session, status: TaskStatus) -> Task:
    task = Task(source_type=SourceType.local, source_license=SourceLicense.own, status=status)
    db.add(task)
    db.flush()
    return task


def test_published_task_render_is_canceled_before_claim(db: Session) -> None:
    task = _task(db, TaskStatus.published)
    job = RenderJob(task_id=task.id, status=RenderJobStatus.queued, request_json={})
    db.add(job)
    db.flush()

    detail = _cancel_unclaimable_render_job(db, job, task, datetime.now(timezone.utc))

    assert detail == "task already published"
    assert job.status == RenderJobStatus.canceled


def test_newer_duplicate_render_is_canceled_before_claim(db: Session) -> None:
    task = _task(db, TaskStatus.subtitle_ready)
    now = datetime.now(timezone.utc)
    older = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.queued,
        request_json={},
        created_at=now - timedelta(minutes=1),
    )
    db.add(older)
    db.flush()
    newer = RenderJob(task_id=task.id, status=RenderJobStatus.queued, request_json={}, created_at=now)
    db.add(newer)
    db.flush()

    detail = _cancel_unclaimable_render_job(db, newer, task, now)

    assert detail == f"superseded by render job {older.id}"
    assert older.status == RenderJobStatus.queued
    assert newer.status == RenderJobStatus.canceled


def test_live_render_wins_over_an_older_queued_render(db: Session) -> None:
    task = _task(db, TaskStatus.subtitle_ready)
    now = datetime.now(timezone.utc)
    older = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.queued,
        request_json={},
        created_at=now - timedelta(minutes=1),
    )
    live = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.running,
        request_json={},
        created_at=now,
        lease_owner="worker-a",
        lease_until=now + timedelta(minutes=5),
    )
    db.add_all([older, live])
    db.flush()

    detail = _cancel_unclaimable_render_job(db, older, task, now)

    assert detail == f"superseded by render job {live.id}"
    assert older.status == RenderJobStatus.canceled
    assert live.status == RenderJobStatus.running
