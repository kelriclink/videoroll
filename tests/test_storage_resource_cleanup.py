from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.services import maintenance_service
from videoroll.db.base import Base
from videoroll.db.models import AppSetting, Asset, AssetKind, SourceLicense, SourceType, Subtitle, SubtitleFormat, Task, TaskStatus


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


class _FakeS3:
    def __init__(self, keys: set[str]) -> None:
        self.keys = set(keys)
        self.deleted: set[str] = set()

    def ensure_bucket(self) -> None:
        return None

    @property
    def bucket(self) -> str:
        return "videoroll"

    def list_bucket_names(self) -> list[str]:
        return [self.bucket]

    def iter_object_keys(self, prefix: str = "", *, bucket: str | None = None):
        assert bucket == self.bucket
        yield from sorted(key for key in self.keys if key.startswith(prefix))

    def delete_objects(self, keys: list[str], *, bucket: str | None = None) -> tuple[set[str], set[str]]:
        assert bucket == self.bucket
        deleted = set(keys)
        self.deleted.update(deleted)
        self.keys.difference_update(deleted)
        return deleted, set()


class _LegacyNamespaceFakeS3:
    bucket = "videoroll"

    def __init__(self, keys_by_bucket: dict[str, set[str]]) -> None:
        self.keys_by_bucket = {bucket: set(keys) for bucket, keys in keys_by_bucket.items()}

    def list_bucket_names(self) -> list[str]:
        return sorted(self.keys_by_bucket)

    def iter_object_keys(self, prefix: str = "", *, bucket: str | None = None):
        yield from sorted(key for key in self.keys_by_bucket[bucket or self.bucket] if key.startswith(prefix))

    def delete_objects(self, keys: list[str], *, bucket: str | None = None) -> tuple[set[str], set[str]]:
        target = bucket or self.bucket
        deleted = set(keys)
        self.keys_by_bucket[target].difference_update(deleted)
        return deleted, set()


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AppSetting.__table__, Task.__table__, Asset.__table__, Subtitle.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine, tables=[Subtitle.__table__, Asset.__table__, Task.__table__, AppSetting.__table__])


def _task(status: TaskStatus) -> Task:
    return Task(source_type=SourceType.local, source_license=SourceLicense.own, status=status)


def test_expire_stale_publishing_tasks_marks_only_overdue_tasks_failed(db: Session) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    overdue = _task(TaskStatus.publishing)
    overdue.updated_at = now - timedelta(hours=49)
    recent = _task(TaskStatus.publishing)
    recent.updated_at = now - timedelta(hours=47)
    already_failed = _task(TaskStatus.failed)
    already_failed.updated_at = now - timedelta(hours=72)
    db.add_all([overdue, recent, already_failed])
    db.commit()

    expired = maintenance_service.expire_stale_publishing_tasks(db, timeout_hours=48, now=now)

    assert expired == 1
    assert overdue.status == TaskStatus.failed
    assert overdue.error_code == maintenance_service.PUBLISHING_TIMEOUT_ERROR_CODE
    assert overdue.error_message == "publishing status exceeded 48 hours"
    assert recent.status == TaskStatus.publishing
    assert already_failed.error_code is None


def test_task_id_from_resource_key_recognizes_only_owned_resource_prefixes() -> None:
    task_id = uuid.uuid4()

    assert maintenance_service.task_id_from_resource_key(f"final/{task_id}/video.mp4") == task_id
    assert maintenance_service.task_id_from_resource_key(f"other/{task_id}/video.mp4") is None
    assert maintenance_service.task_id_from_resource_key("final/not-a-uuid/video.mp4") is None


def test_resource_scan_includes_legacy_namespaced_bucket_objects() -> None:
    task_id = uuid.uuid4()
    fake_s3 = _LegacyNamespaceFakeS3(
        {
            "videoroll": {f"final/{task_id}/video.mp4"},
            "minio": {f"videoroll/raw/{task_id}/source.mp4", "unrelated/file.bin"},
        }
    )

    objects = maintenance_service._task_resource_objects(fake_s3, {task_id})

    assert ("videoroll", f"final/{task_id}/video.mp4") in objects
    assert ("minio", f"videoroll/raw/{task_id}/source.mp4") in objects
    assert ("minio", "unrelated/file.bin") not in objects


def test_cleanup_terminal_resources_deletes_orphans_but_preserves_task_history_and_stopped_work(db: Session) -> None:
    published = _task(TaskStatus.published)
    failed = _task(TaskStatus.failed)
    paused = _task(TaskStatus.canceled)
    paused.stopped_status = TaskStatus.downloaded
    db.add_all([published, failed, paused])
    db.flush()

    published_key = f"final/{published.id}/video.mp4"
    failed_key = f"raw/{failed.id}/source.mp4"
    paused_key = f"raw/{paused.id}/source.mp4"
    db.add_all(
        [
            Asset(task_id=published.id, kind=AssetKind.video_final, storage_key=published_key),
            Asset(task_id=failed.id, kind=AssetKind.video_raw, storage_key=failed_key),
            Asset(task_id=paused.id, kind=AssetKind.video_raw, storage_key=paused_key),
            Subtitle(task_id=published.id, format=SubtitleFormat.srt, language="zh", storage_key=f"sub/{published.id}/subtitle.srt"),
        ]
    )
    db.commit()

    orphan_key = f"raw/{published.id}/orphan.mp4"
    fake_s3 = _FakeS3({published_key, failed_key, paused_key, orphan_key, f"sub/{published.id}/subtitle.srt"})
    with patch.object(maintenance_service, "S3Store", return_value=fake_s3):
        result = maintenance_service.cleanup_terminal_task_resources(
            MagicMock(),
            db,
            published_older_than_days=None,
            failed_older_than_hours=None,
            owner_prefix="test",
            cleanup_all_terminal=True,
        )

    assert result is not None
    assert result.matched_tasks == 2
    assert result.deleted_assets == 2
    assert result.deleted_subtitles == 1
    assert result.deleted_objects == 4
    assert orphan_key in fake_s3.deleted
    assert paused_key not in fake_s3.deleted
    assert db.query(Task).count() == 3
    assert db.query(Asset).filter(Asset.task_id == paused.id).count() == 1


def test_scheduled_cleanup_waits_48_hours_after_failure_even_when_published_retention_is_disabled(db: Session) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    old_failed = _task(TaskStatus.failed)
    old_failed.updated_at = now - timedelta(hours=49)
    recent_failed = _task(TaskStatus.failed)
    recent_failed.updated_at = now - timedelta(hours=47)
    old_published = _task(TaskStatus.published)
    old_published.updated_at = now - timedelta(days=30)
    db.add_all([old_failed, recent_failed, old_published])
    db.flush()

    old_failed_key = f"raw/{old_failed.id}/source.mp4"
    recent_failed_key = f"raw/{recent_failed.id}/source.mp4"
    published_key = f"final/{old_published.id}/video.mp4"
    db.add_all(
        [
            Asset(task_id=old_failed.id, kind=AssetKind.video_raw, storage_key=old_failed_key),
            Asset(task_id=recent_failed.id, kind=AssetKind.video_raw, storage_key=recent_failed_key),
            Asset(task_id=old_published.id, kind=AssetKind.video_final, storage_key=published_key),
        ]
    )
    db.commit()

    fake_s3 = _FakeS3({old_failed_key, recent_failed_key, published_key})
    with patch.object(maintenance_service, "S3Store", return_value=fake_s3):
        result = maintenance_service.cleanup_terminal_task_resources(
            MagicMock(),
            db,
            published_older_than_days=None,
            failed_older_than_hours=48,
            owner_prefix="test",
            now=now,
        )

    assert result is not None
    assert result.matched_tasks == 1
    assert old_failed_key in fake_s3.deleted
    assert recent_failed_key not in fake_s3.deleted
    assert published_key not in fake_s3.deleted
    assert db.query(Asset).filter(Asset.task_id == old_failed.id).count() == 0
    assert db.query(Asset).filter(Asset.task_id == recent_failed.id).count() == 1
    assert db.query(Asset).filter(Asset.task_id == old_published.id).count() == 1
