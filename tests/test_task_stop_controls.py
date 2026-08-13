from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.services import task_service
from videoroll.db.base import Base
from videoroll.db.models import (
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
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__, RenderJob.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine, tables=[RenderJob.__table__, SubtitleJob.__table__, Task.__table__])


def _task(status: TaskStatus = TaskStatus.ingested) -> Task:
    return Task(source_type=SourceType.local, source_license=SourceLicense.own, status=status)


def test_stop_and_resume_preserves_the_previous_task_stage(db: Session) -> None:
    task = _task(TaskStatus.translated)
    task.lock_owner = "subtitle_service.task_queue"
    task.lock_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.add(task)
    db.commit()

    stopped = task_service.stop_task(task.id, db=db)

    assert stopped.status == TaskStatus.canceled
    assert stopped.stopped_status == TaskStatus.translated
    assert stopped.lock_owner is None
    assert stopped.lock_until is None

    resumed = task_service.resume_stopped_task(task.id, db=db)

    assert resumed.status == TaskStatus.translated
    assert resumed.stopped_status is None


def test_stop_all_skips_terminal_and_already_stopped_tasks(db: Session) -> None:
    active = _task(TaskStatus.downloaded)
    completed = _task(TaskStatus.published)
    failed = _task(TaskStatus.failed)
    already_stopped = _task(TaskStatus.canceled)
    already_stopped.stopped_status = TaskStatus.ingested
    already_stopped.lock_owner = "subtitle_service.task_queue"
    already_stopped.lock_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.add_all([active, completed, failed, already_stopped])
    db.commit()

    matched_count, changed_count = task_service.stop_all_tasks(db=db)

    assert (matched_count, changed_count) == (1, 1)
    assert active.status == TaskStatus.canceled
    assert active.stopped_status == TaskStatus.downloaded
    assert completed.status == TaskStatus.published
    assert failed.status == TaskStatus.failed
    assert already_stopped.lock_owner is None
    assert already_stopped.lock_until is None


def test_stop_all_includes_failed_tasks_with_live_jobs(db: Session) -> None:
    failed_with_subtitle = _task(TaskStatus.failed)
    failed_with_render = _task(TaskStatus.failed)
    failed_without_live_jobs = _task(TaskStatus.failed)
    db.add_all([failed_with_subtitle, failed_with_render, failed_without_live_jobs])
    db.flush()
    db.add(
        SubtitleJob(
            task_id=failed_with_subtitle.id,
            status=SubtitleJobStatus.running,
            request_json={},
        )
    )
    db.add(
        RenderJob(
            task_id=failed_with_render.id,
            status=RenderJobStatus.queued,
            request_json={},
        )
    )
    db.commit()

    matched_count, changed_count = task_service.stop_all_tasks(db=db)

    assert (matched_count, changed_count) == (2, 2)
    assert failed_with_subtitle.status == TaskStatus.canceled
    assert failed_with_subtitle.stopped_status == TaskStatus.failed
    assert failed_with_render.status == TaskStatus.canceled
    assert failed_with_render.stopped_status == TaskStatus.failed
    assert failed_without_live_jobs.status == TaskStatus.failed


def test_resume_all_only_resumes_tasks_stopped_by_task_controls(db: Session) -> None:
    resumable = _task(TaskStatus.subtitle_ready)
    resumable.status = TaskStatus.canceled
    resumable.stopped_status = TaskStatus.subtitle_ready
    legacy_canceled = _task(TaskStatus.canceled)
    db.add_all([resumable, legacy_canceled])
    db.commit()

    matched_count, changed_count, resumed = task_service.resume_all_stopped_tasks(db=db)

    assert (matched_count, changed_count) == (1, 1)
    assert [task.id for task in resumed] == [resumable.id]
    assert resumable.status == TaskStatus.subtitle_ready
    assert resumable.stopped_status is None
    assert legacy_canceled.status == TaskStatus.canceled
