from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from videoroll.apps.subtitle_service import main as subtitle_main
from videoroll.db.base import Base
from videoroll.db.models import RenderJob, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def test_hatchet_queue_projection_uses_durable_job_state(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__, RenderJob.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setenv("HATCHET_SUBTITLE_WORKER_SLOTS", "3")

    with factory() as db:
        queued_task = Task(
            source_type=SourceType.local,
            source_license=SourceLicense.own,
            status=TaskStatus.downloaded,
            priority=100,
        )
        running_task = Task(
            source_type=SourceType.local,
            source_license=SourceLicense.own,
            status=TaskStatus.audio_extracted,
            priority=0,
        )
        db.add_all([queued_task, running_task])
        db.flush()
        db.add_all([
            SubtitleJob(task_id=queued_task.id, status=SubtitleJobStatus.queued, request_json={}, progress=0),
            SubtitleJob(task_id=running_task.id, status=SubtitleJobStatus.running, request_json={}, progress=55),
        ])
        db.commit()
        queued_task_id = queued_task.id
        running_task_id = running_task.id

        queue = subtitle_main._read_task_queue(db, limit=100)

    assert queue.settings.scheduler == "hatchet"
    assert queue.settings.subtitle_worker_slots == 3
    assert queue.running_count == 1
    assert queue.queued_count == 1
    by_task = {item.task_id: item for item in queue.tasks}
    assert by_task[queued_task_id].state == "queued"
    assert by_task[queued_task_id].stage == "subtitle"
    assert by_task[running_task_id].state == "running"
    assert by_task[running_task_id].progress == 55
    engine.dispose()
