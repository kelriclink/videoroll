from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.schemas import WorkdirMaintenanceEntryRead, WorkdirMaintenanceRead
from videoroll.config import OrchestratorSettings
from videoroll.db.models import (
    AppSetting,
    RenderJob,
    RenderJobStatus,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
)
from videoroll.db.session import get_sessionmaker
from videoroll.utils.workdir_maintenance import WorkdirJobState, cleanup_reclaimable_dirs, scan_workdir


WORKDIR_LOCK_KEY = "orchestrator.workdir_maintenance"
WORKDIR_RECENT_GRACE_SECONDS = int(os.getenv("WORKDIR_RECENT_GRACE_SECONDS", "900") or "900")
WORKDIR_LOCK_TTL_SECONDS = int(os.getenv("WORKDIR_MAINTENANCE_LOCK_TTL_SECONDS", "900") or "900")


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
