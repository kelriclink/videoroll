from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, SourceLicense, SourceType, Task, TaskStatus
from videoroll.storage.filesystem import FileStore
from videoroll.storage.recovery import repair_incomplete_tasks


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def _store(tmp_path) -> FileStore:
    store = FileStore(SimpleNamespace(storage_root=str(tmp_path / "objects")))
    store.ensure_ready()
    return store


def test_recovery_dry_run_does_not_change_legacy_failed_task(tmp_path) -> None:
    db = _session()
    store = _store(tmp_path)
    task = Task(source_type=SourceType.youtube, source_license=SourceLicense.own, status=TaskStatus.failed)
    db.add(task)
    db.flush()
    db.add(Asset(task_id=task.id, kind=AssetKind.video_raw, storage_key=f"raw/{task.id}/missing.webm"))
    db.commit()

    summary = repair_incomplete_tasks(db, store, dry_run=True)

    assert summary.reset_youtube == 1
    assert db.get(Task, task.id).status == TaskStatus.failed
    assert db.query(Asset).filter(Asset.task_id == task.id).count() == 1


def test_recovery_resets_missing_source_and_preserves_published(tmp_path) -> None:
    db = _session()
    store = _store(tmp_path)
    failed = Task(source_type=SourceType.youtube, source_license=SourceLicense.own, status=TaskStatus.failed)
    published = Task(source_type=SourceType.youtube, source_license=SourceLicense.own, status=TaskStatus.published)
    db.add_all([failed, published])
    db.flush()
    db.add_all(
        [
            Asset(task_id=failed.id, kind=AssetKind.video_raw, storage_key=f"raw/{failed.id}/missing.webm"),
            Asset(task_id=published.id, kind=AssetKind.video_raw, storage_key=f"raw/{published.id}/missing.webm"),
        ]
    )
    db.commit()

    summary = repair_incomplete_tasks(db, store, dry_run=False)

    assert summary.reset_youtube == 1
    assert db.get(Task, failed.id).status == TaskStatus.ingested
    assert db.query(Asset).filter(Asset.task_id == failed.id).count() == 0
    assert db.get(Task, published.id).status == TaskStatus.published
    assert db.query(Asset).filter(Asset.task_id == published.id).count() == 1
