from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.schemas import StorageResourceCleanupRead, WorkdirMaintenanceEntryRead, WorkdirMaintenanceRead
from videoroll.apps.orchestrator_api.services import asset_service
from videoroll.config import OrchestratorSettings
from videoroll.db.models import (
    AppSetting,
    Asset,
    RenderJob,
    RenderJobStatus,
    Subtitle,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.utils.workdir_maintenance import WorkdirJobState, cleanup_reclaimable_dirs, scan_workdir


WORKDIR_LOCK_KEY = "orchestrator.workdir_maintenance"
STORAGE_RESOURCE_CLEANUP_LOCK_KEY = "orchestrator.storage_resource_cleanup"
WORKDIR_RECENT_GRACE_SECONDS = int(os.getenv("WORKDIR_RECENT_GRACE_SECONDS", "900") or "900")
WORKDIR_LOCK_TTL_SECONDS = int(os.getenv("WORKDIR_MAINTENANCE_LOCK_TTL_SECONDS", "900") or "900")
STORAGE_RESOURCE_CLEANUP_LOCK_TTL_SECONDS = int(os.getenv("STORAGE_RESOURCE_CLEANUP_LOCK_TTL_SECONDS", "3600") or "3600")
TASK_RESOURCE_PREFIXES = ("raw", "final", "sub", "work", "log", "meta")
S3_DELETE_BATCH_SIZE = 1000
PUBLISHING_TIMEOUT_ERROR_CODE = "PUBLISHING_TIMEOUT"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def workdir_lock_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, WORKDIR_LOCK_KEY)
    if row is None:
        row = AppSetting(key=WORKDIR_LOCK_KEY, value_json={})
        db.add(row)
        db.commit()
    locked = db.query(AppSetting).filter(AppSetting.key == WORKDIR_LOCK_KEY).with_for_update().first()
    if locked is None:
        raise RuntimeError("failed to lock workdir maintenance row")
    return locked


def try_acquire_workdir_lock(db: Session, *, owner: str, ttl_seconds: int) -> bool:
    row = workdir_lock_row(db)
    data = dict(row.value_json or {})
    now = utcnow()
    lock_owner = str(data.get("lock_owner") or "").strip()
    lock_until = parse_iso_datetime(data.get("lock_until"))
    if lock_owner and lock_until and lock_until > now and lock_owner != owner:
        return False
    data["lock_owner"] = owner
    data["lock_until"] = (now + timedelta(seconds=max(30, int(ttl_seconds or 0)))).isoformat()
    row.value_json = data
    db.add(row)
    db.commit()
    return True


def release_workdir_lock(db: Session, *, owner: str) -> None:
    row = workdir_lock_row(db)
    data = dict(row.value_json or {})
    lock_owner = str(data.get("lock_owner") or "").strip()
    if lock_owner and lock_owner != owner:
        return
    data.pop("lock_owner", None)
    data.pop("lock_until", None)
    row.value_json = data
    db.add(row)
    db.commit()


def _resource_cleanup_lock_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, STORAGE_RESOURCE_CLEANUP_LOCK_KEY)
    if row is None:
        row = AppSetting(key=STORAGE_RESOURCE_CLEANUP_LOCK_KEY, value_json={})
        db.add(row)
        db.commit()
    locked = db.query(AppSetting).filter(AppSetting.key == STORAGE_RESOURCE_CLEANUP_LOCK_KEY).with_for_update().first()
    if locked is None:
        raise RuntimeError("failed to lock storage resource cleanup row")
    return locked


def _try_acquire_resource_cleanup_lock(db: Session, *, owner: str, ttl_seconds: int) -> bool:
    row = _resource_cleanup_lock_row(db)
    data = dict(row.value_json or {})
    now = utcnow()
    lock_owner = str(data.get("lock_owner") or "").strip()
    lock_until = parse_iso_datetime(data.get("lock_until"))
    if lock_owner and lock_until and lock_until > now and lock_owner != owner:
        return False
    data["lock_owner"] = owner
    data["lock_until"] = (now + timedelta(seconds=max(30, int(ttl_seconds or 0)))).isoformat()
    row.value_json = data
    db.add(row)
    db.commit()
    return True


def _release_resource_cleanup_lock(db: Session, *, owner: str) -> None:
    row = _resource_cleanup_lock_row(db)
    data = dict(row.value_json or {})
    lock_owner = str(data.get("lock_owner") or "").strip()
    if lock_owner and lock_owner != owner:
        return
    data.pop("lock_owner", None)
    data.pop("lock_until", None)
    row.value_json = data
    db.add(row)
    db.commit()


def task_id_from_resource_key(storage_key: str) -> uuid.UUID | None:
    parts = str(storage_key or "").split("/", 2)
    if len(parts) < 2 or parts[0] not in TASK_RESOURCE_PREFIXES:
        return None
    try:
        return uuid.UUID(parts[1])
    except (TypeError, ValueError):
        return None


def expire_stale_publishing_tasks(
    db: Session,
    *,
    timeout_hours: int,
    now: datetime | None = None,
    limit: int = 500,
) -> int:
    """Fail tasks that have remained in PUBLISHING beyond the deadline."""
    effective_now = now or utcnow()
    effective_timeout_hours = max(1, int(timeout_hours or 0))
    cutoff = effective_now - timedelta(hours=effective_timeout_hours)
    tasks = (
        db.query(Task)
        .filter(Task.status == TaskStatus.publishing, Task.updated_at < cutoff)
        .order_by(Task.updated_at.asc(), Task.id.asc())
        .with_for_update(skip_locked=True)
        .limit(max(1, min(int(limit or 1), 5000)))
        .all()
    )
    for task in tasks:
        task.status = TaskStatus.failed
        task.error_code = PUBLISHING_TIMEOUT_ERROR_CODE
        task.error_message = f"publishing status exceeded {effective_timeout_hours} hours"
        task.lock_owner = None
        task.lock_until = None
        db.add(task)
    db.commit()
    return len(tasks)


def _all_terminal_task_filter() -> Any:
    # A CANCELED task with stopped_status is paused and can be resumed, so it
    # is deliberately excluded from resource deletion.
    return or_(
        Task.status.in_([TaskStatus.published, TaskStatus.failed]),
        and_(Task.status == TaskStatus.canceled, Task.stopped_status.is_(None)),
    )


def _scheduled_resource_cleanup_filter(
    *,
    now: datetime,
    published_older_than_days: int | None,
    failed_older_than_hours: int | None,
) -> Any:
    filters: list[Any] = []
    if published_older_than_days is not None:
        published_cutoff = now - timedelta(days=max(0, int(published_older_than_days)))
        filters.append(
            and_(
                or_(
                    Task.status == TaskStatus.published,
                    and_(Task.status == TaskStatus.canceled, Task.stopped_status.is_(None)),
                ),
                Task.updated_at < published_cutoff,
            )
        )
    if failed_older_than_hours is not None:
        failed_cutoff = now - timedelta(hours=max(0, int(failed_older_than_hours)))
        filters.append(and_(Task.status == TaskStatus.failed, Task.updated_at < failed_cutoff))
    if not filters:
        return Task.id.is_(None)
    return or_(*filters)


def _task_resource_objects(s3: S3Store, task_ids: set[uuid.UUID]) -> set[tuple[str, str]]:
    """Find current and namespaced legacy MinIO objects belonging to task IDs."""
    if not task_ids:
        return set()
    try:
        buckets = set(s3.list_bucket_names())
    except Exception:
        buckets = set()
    buckets.add(s3.bucket)

    objects: set[tuple[str, str]] = set()
    for bucket in buckets:
        # Current layouts use bucket/raw/<task-id>/..., while an older
        # deployment wrote the same application paths under minio/videoroll/.
        namespace = "" if bucket == s3.bucket else f"{s3.bucket}/"
        for prefix in TASK_RESOURCE_PREFIXES:
            for key in s3.iter_object_keys(f"{namespace}{prefix}/", bucket=bucket):
                relative_key = key[len(namespace) :] if namespace and key.startswith(namespace) else key
                if task_id_from_resource_key(relative_key) in task_ids:
                    objects.add((bucket, key))
    return objects


def _delete_s3_objects(s3: S3Store, objects: set[tuple[str, str]]) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    deleted: set[tuple[str, str]] = set()
    failed: set[tuple[str, str]] = set()
    by_bucket: dict[str, list[str]] = {}
    for bucket, key in objects:
        by_bucket.setdefault(bucket, []).append(key)
    for bucket, keys in by_bucket.items():
        ordered_keys = sorted(keys)
        for offset in range(0, len(ordered_keys), S3_DELETE_BATCH_SIZE):
            batch = ordered_keys[offset : offset + S3_DELETE_BATCH_SIZE]
            batch_deleted, batch_failed = s3.delete_objects(batch, bucket=bucket)
            deleted.update((bucket, key) for key in batch_deleted)
            failed.update((bucket, key) for key in batch_failed)
    return deleted, failed


def cleanup_terminal_task_resources(
    settings: OrchestratorSettings,
    db: Session,
    *,
    published_older_than_days: int | None,
    failed_older_than_hours: int | None,
    owner_prefix: str,
    cleanup_all_terminal: bool = False,
    now: datetime | None = None,
) -> StorageResourceCleanupRead | None:
    """Delete every MinIO/S3 resource for completed tasks, retaining task rows.

    Prefix scanning removes old orphan objects too.  Older cleanup versions
    removed database asset rows without reliably deleting their MinIO objects;
    those orphaned raw/final files cannot be found by querying ``assets``.
    """
    owner = f"{owner_prefix}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    if not _try_acquire_resource_cleanup_lock(db, owner=owner, ttl_seconds=STORAGE_RESOURCE_CLEANUP_LOCK_TTL_SECONDS):
        return None
    try:
        task_filter = (
            _all_terminal_task_filter()
            if cleanup_all_terminal
            else _scheduled_resource_cleanup_filter(
                now=now or utcnow(),
                published_older_than_days=published_older_than_days,
                failed_older_than_hours=failed_older_than_hours,
            )
        )
        query = db.query(Task.id).filter(task_filter)
        task_ids = {row[0] for row in query.all()}
        if not task_ids:
            return StorageResourceCleanupRead()

        asset_keys = {
            str(row[0])
            for row in db.query(Asset.storage_key).filter(Asset.task_id.in_(task_ids)).distinct().all()
            if str(row[0] or "").strip()
        }
        matched_assets = db.query(Asset).filter(Asset.task_id.in_(task_ids)).count()
        matched_subtitles = db.query(Subtitle).filter(Subtitle.task_id.in_(task_ids)).count()

        s3 = S3Store(settings)
        s3.ensure_bucket()
        object_locations = _task_resource_objects(s3, task_ids)
        object_locations.update((s3.bucket, key) for key in asset_keys)
        deleted_keys, failed_keys = _delete_s3_objects(s3, object_locations)

        for bucket, key in failed_keys:
            asset_service.queue_pending_s3_delete(
                db,
                key,
                bucket=bucket,
                reason="terminal_task_resource_cleanup",
                commit=False,
            )
        deleted_subtitles = db.query(Subtitle).filter(Subtitle.task_id.in_(task_ids)).delete(synchronize_session=False)
        deleted_assets = db.query(Asset).filter(Asset.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.commit()

        retried_objects = 0
        if failed_keys:
            retried_objects = asset_service.retry_pending_s3_deletes(db, s3, limit=len(failed_keys))
        return StorageResourceCleanupRead(
            matched_tasks=len(task_ids),
            matched_assets=matched_assets,
            matched_subtitles=matched_subtitles,
            deleted_assets=int(deleted_assets or 0),
            deleted_subtitles=int(deleted_subtitles or 0),
            deleted_objects=len(deleted_keys) + retried_objects,
            pending_objects=max(0, len(failed_keys) - retried_objects),
        )
    finally:
        try:
            _release_resource_cleanup_lock(db, owner=owner)
        except Exception:
            db.rollback()


def collect_named_dirs(root: Path) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    if not root.is_dir():
        return out
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            out.add(uuid.UUID(child.name))
        except Exception:
            continue
    return out


def scan_workdir_state(settings: OrchestratorSettings, db: Session) -> Any:
    work_dir = Path(settings.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    now = utcnow()

    subtitle_job_ids = collect_named_dirs(work_dir / "subtitle")
    render_job_ids = collect_named_dirs(work_dir / "render")
    youtube_task_ids = collect_named_dirs(work_dir / "youtube")

    subtitle_jobs: dict[uuid.UUID, WorkdirJobState] = {}
    if subtitle_job_ids:
        for row in db.query(SubtitleJob).filter(SubtitleJob.id.in_(subtitle_job_ids)).all():
            subtitle_jobs[row.id] = WorkdirJobState(
                task_id=row.task_id,
                status=str(row.status.value if hasattr(row.status, "value") else row.status),
            )

    render_jobs: dict[uuid.UUID, WorkdirJobState] = {}
    if render_job_ids:
        for row in db.query(RenderJob).filter(RenderJob.id.in_(render_job_ids)).all():
            render_jobs[row.id] = WorkdirJobState(
                task_id=row.task_id,
                status=str(row.status.value if hasattr(row.status, "value") else row.status),
            )

    known_task_ids: set[uuid.UUID] = set()
    active_task_ids: set[uuid.UUID] = set()
    if youtube_task_ids:
        for row in db.query(Task).filter(Task.id.in_(youtube_task_ids)).all():
            known_task_ids.add(row.id)
            if row.lock_owner and row.lock_until and row.lock_until > now:
                active_task_ids.add(row.id)
        for task_id, in (
            db.query(SubtitleJob.task_id)
            .filter(
                SubtitleJob.task_id.in_(youtube_task_ids),
                SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
            )
            .distinct()
            .all()
        ):
            active_task_ids.add(task_id)
        for task_id, in (
            db.query(RenderJob.task_id)
            .filter(
                RenderJob.task_id.in_(youtube_task_ids),
                RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
            )
            .distinct()
            .all()
        ):
            active_task_ids.add(task_id)

    return scan_workdir(
        work_dir,
        subtitle_jobs=subtitle_jobs,
        render_jobs=render_jobs,
        known_task_ids=known_task_ids,
        active_task_ids=active_task_ids,
        now=now,
        recent_grace_seconds=WORKDIR_RECENT_GRACE_SECONDS,
    )


def workdir_scan_to_read(
    scan: Any,
    *,
    deleted_dirs: int = 0,
    deleted_bytes: int = 0,
    deleted_paths: list[str] | None = None,
    errors: list[str] | None = None,
) -> WorkdirMaintenanceRead:
    return WorkdirMaintenanceRead(
        work_dir=scan.work_dir,
        scanned_dirs=scan.scanned_dirs,
        reclaimable_dirs=scan.reclaimable_dirs,
        total_bytes=scan.total_bytes,
        reclaimable_bytes=scan.reclaimable_bytes,
        deleted_dirs=deleted_dirs,
        deleted_bytes=deleted_bytes,
        deleted_paths=list(deleted_paths or []),
        errors=list(errors or []),
        entries=[
            WorkdirMaintenanceEntryRead(
                kind=entry.kind,
                owner_id=entry.owner_id,
                rel_path=entry.rel_path,
                size_bytes=entry.size_bytes,
                modified_at=entry.modified_at,
                reclaimable=entry.reclaimable,
                reason=entry.reason,
                task_id=uuid.UUID(entry.task_id) if entry.task_id else None,
            )
            for entry in scan.entries
        ],
    )


def cleanup_workdir(settings: OrchestratorSettings, db: Session, *, owner_prefix: str) -> WorkdirMaintenanceRead | None:
    owner = f"{owner_prefix}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    if not try_acquire_workdir_lock(db, owner=owner, ttl_seconds=WORKDIR_LOCK_TTL_SECONDS):
        return None
    try:
        scan_before = scan_workdir_state(settings, db)
        cleanup = cleanup_reclaimable_dirs(Path(settings.work_dir), scan_before.entries)
        scan_after = scan_workdir_state(settings, db)
        return workdir_scan_to_read(
            scan_after,
            deleted_dirs=cleanup.deleted_dirs,
            deleted_bytes=cleanup.deleted_bytes,
            deleted_paths=cleanup.deleted_paths,
            errors=cleanup.errors,
        )
    finally:
        try:
            release_workdir_lock(db, owner=owner)
        except Exception:
            db.rollback()


def run_startup_workdir_cleanup(settings: OrchestratorSettings) -> WorkdirMaintenanceRead | None:
    session_local = get_sessionmaker(settings.database_url)
    db = session_local()
    try:
        return cleanup_workdir(settings, db, owner_prefix="startup")
    finally:
        db.close()
