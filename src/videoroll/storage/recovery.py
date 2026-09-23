"""Repair task rows left behind by the MinIO to filesystem storage switch.

The database deliberately keeps task history, but object keys from the old
backend are not readable after the switch.  This module removes only stale
working state from unfinished tasks and leaves published history untouched.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from typing import Iterable

from sqlalchemy.orm import Session

from videoroll.config import get_orchestrator_settings
from videoroll.db.models import (
    Asset,
    PublishBatch,
    PublishJob,
    RenderJob,
    SourceType,
    Subtitle,
    SubtitleJob,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_sessionmaker
from videoroll.storage.filesystem import FileStore, StorageObjectNotFound


logger = logging.getLogger(__name__)


@dataclass
class RecoverySummary:
    inspected: int = 0
    published_preserved: int = 0
    canceled_preserved: int = 0
    publishing_deferred: int = 0
    reset_youtube: int = 0
    reset_local: int = 0
    missing_assets: int = 0
    dry_run: bool = True


def asset_exists(store: FileStore, storage_key: str) -> bool:
    try:
        store.head_object(storage_key)
    except (StorageObjectNotFound, OSError, ValueError):
        return False
    return True


def clear_task_working_state(db: Session, task: Task) -> None:
    """Delete artifacts/jobs that cannot be resumed without their source file."""
    db.query(PublishJob).filter(PublishJob.task_id == task.id).delete(synchronize_session=False)
    db.query(RenderJob).filter(RenderJob.task_id == task.id).delete(synchronize_session=False)
    db.query(SubtitleJob).filter(SubtitleJob.task_id == task.id).delete(synchronize_session=False)
    db.query(PublishBatch).filter(PublishBatch.task_id == task.id).delete(synchronize_session=False)
    db.query(Subtitle).filter(Subtitle.task_id == task.id).delete(synchronize_session=False)
    db.query(Asset).filter(Asset.task_id == task.id).delete(synchronize_session=False)

    task.active_publish_batch_id = None
    task.stopped_status = None
    task.error_code = None
    task.error_message = None
    task.retry_count = 0


def reset_task_for_source_recovery(db: Session, task: Task) -> TaskStatus:
    """Reset an unfinished task to the first stage that can obtain its source."""
    clear_task_working_state(db, task)
    next_status = TaskStatus.ingested if task.source_type == SourceType.youtube else TaskStatus.created
    task.status = next_status
    db.add(task)
    return next_status


def _iter_tasks(db: Session, limit: int | None) -> Iterable[Task]:
    query = db.query(Task).order_by(Task.created_at.asc())
    if limit is not None:
        query = query.limit(max(1, int(limit)))
    return query.all()


def repair_incomplete_tasks(
    db: Session,
    store: FileStore,
    *,
    dry_run: bool = True,
    limit: int | None = None,
) -> RecoverySummary:
    """Repair unfinished rows whose referenced filesystem objects are missing.

    Published and canceled tasks are never changed. Publishing tasks are also
    deferred because an external submission may already have started. Failed
    or in-progress tasks are reset only when at least one asset is missing; a
    failed task with complete local assets can still use the normal resume flow.
    """
    summary = RecoverySummary(dry_run=dry_run)
    for task in _iter_tasks(db, limit):
        summary.inspected += 1
        if task.status == TaskStatus.published:
            summary.published_preserved += 1
            continue
        if task.status == TaskStatus.canceled:
            summary.canceled_preserved += 1
            continue

        assets = db.query(Asset).filter(Asset.task_id == task.id).all()
        missing = [asset for asset in assets if not asset_exists(store, asset.storage_key)]
        summary.missing_assets += len(missing)
        if task.status == TaskStatus.publishing:
            if missing:
                summary.publishing_deferred += 1
            continue
        if not missing and assets:
            continue
        if not missing and task.status in {TaskStatus.created, TaskStatus.ingested}:
            continue

        if task.source_type == SourceType.youtube:
            summary.reset_youtube += 1
        else:
            summary.reset_local += 1
        if not dry_run:
            reset_task_for_source_recovery(db, task)

    if not dry_run:
        db.commit()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply the repair; default is a dry run")
    parser.add_argument("--limit", type=int, default=None, help="inspect at most this many unfinished tasks")
    args = parser.parse_args()

    settings = get_orchestrator_settings()
    store = FileStore(settings)
    store.ensure_ready()
    db = get_sessionmaker(settings.database_url)()
    try:
        summary = repair_incomplete_tasks(db, store, dry_run=not args.apply, limit=args.limit)
        print(json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
