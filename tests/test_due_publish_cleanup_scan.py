from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from videoroll.apps.publish_lifecycle import due_missing_publish_cleanup_batches
from videoroll.db.base import Base
from videoroll.db.models import (
    OutboxEvent, PublishBatch, SourceLicense, SourceType, Task, TaskStatus,
)


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def test_cleanup_recovery_only_checks_due_batches_with_no_outbox_intent() -> None:
    engine = create_engine("sqlite:///:memory:")
    tables = [Task.__table__, PublishBatch.__table__, OutboxEvent.__table__]
    Base.metadata.create_all(engine, tables=tables)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own, status=TaskStatus.published)
        db.add(task)
        db.flush()
        missing = PublishBatch(task_id=task.id, state="succeeded", finished_at=now - timedelta(hours=48))
        future = PublishBatch(task_id=task.id, state="succeeded", finished_at=now - timedelta(hours=1))
        existing = PublishBatch(task_id=task.id, state="succeeded", finished_at=now - timedelta(hours=48))
        db.add_all([missing, future, existing])
        db.flush()
        db.add(OutboxEvent(
            event_type="publish.cleanup", aggregate_type="publish_batch",
            # SQLite's UUID VARCHAR cast is hex without dashes; Postgres emits canonical UUID text.
            aggregate_id=existing.id.hex,
            task_name="subtitle_service.cleanup_task", operation_key=f"publish-cleanup:{existing.id}",
            args_json={}, status="pending", available_at=now + timedelta(days=1),
        ))
        db.commit()
        results = due_missing_publish_cleanup_batches(db, now=now)
        assert [batch.id for batch in results] == [missing.id]
    engine.dispose()
