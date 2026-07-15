from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.services import task_service
from videoroll.db.base import Base
from videoroll.db.models import RenderJob, SourceLicense, SourceType, SubtitleJob, Task, TaskStatus


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
    db.add(task)
    db.commit()

    stopped = task_service.stop_task(task.id, db=db)

    assert stopped.status == TaskStatus.canceled
    assert stopped.stopped_status == TaskStatus.translated

    resumed = task_service.resume_stopped_task(task.id, db=db)

    assert resumed.status == TaskStatus.translated
    assert resumed.stopped_status is None


def test_stop_all_skips_terminal_and_already_stopped_tasks(db: Session) -> None:
    active = _task(TaskStatus.downloaded)
    completed = _task(TaskStatus.published)
    failed = _task(TaskStatus.failed)
    already_stopped = _task(TaskStatus.canceled)
    already_stopped.stopped_status = TaskStatus.ingested
    db.add_all([active, completed, failed, already_stopped])
    db.commit()

    matched_count, changed_count = task_service.stop_all_tasks(db=db)

    assert (matched_count, changed_count) == (1, 1)
    assert active.status == TaskStatus.canceled
    assert active.stopped_status == TaskStatus.downloaded
    assert completed.status == TaskStatus.published
    assert failed.status == TaskStatus.failed


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
