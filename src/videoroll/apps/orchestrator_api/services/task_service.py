from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.schemas import TaskCreate, TaskRead
from videoroll.apps.orchestrator_api.services import asset_service, publishing_service
from videoroll.apps.subtitle_service.task_title_store import get_task_display_title_with_storage
from videoroll.db.models import (
    AppSetting,
    Asset,
    AssetKind,
    Platform,
    PublishJob,
    PublishState,
    RenderJob,
    RenderJobStatus,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.storage.filesystem import FileStore
from videoroll.utils.auto_youtube import parse_auto_youtube_created_by


STOPPABLE_TASK_STATUSES = (
    TaskStatus.created,
    TaskStatus.ingested,
    TaskStatus.downloaded,
    TaskStatus.audio_extracted,
    TaskStatus.asr_done,
    TaskStatus.translated,
    TaskStatus.subtitle_ready,
    TaskStatus.rendered,
    TaskStatus.ready_for_review,
    TaskStatus.approved,
)


def task_title_key(task_id: uuid.UUID) -> str:
    return f"task.title.{task_id}"


def extract_metadata_title(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return ""
    info = parsed if isinstance(parsed, dict) else {}
    return str(info.get("title") or info.get("fulltitle") or info.get("alt_title") or "").strip()


def load_task_display_titles(
    db: Session,
    task_ids: list[uuid.UUID],
    *,
    store: FileStore | None = None,
    allow_storage_fallback: bool,
) -> dict[uuid.UUID, str]:
    title_map: dict[uuid.UUID, str] = {}
    if not task_ids:
        return title_map

    rows = db.query(AppSetting).filter(AppSetting.key.in_([task_title_key(task_id) for task_id in task_ids])).all()
    by_key = {str(row.key): asset_service.as_dict(getattr(row, "value_json", None)) for row in rows}
    for task_id in task_ids:
        data = by_key.get(task_title_key(task_id)) or {}
        for key in ("translated_title", "source_title"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                title_map[task_id] = value.strip()
                break

    if not allow_storage_fallback or store is None:
        return title_map

    missing = [task_id for task_id in task_ids if task_id not in title_map]
    if not missing:
        return title_map
    assets = (
        db.query(Asset)
        .filter(Asset.task_id.in_(missing), Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .all()
    )
    picked: dict[uuid.UUID, Asset] = {}
    for asset in assets:
        picked.setdefault(asset.task_id, asset)
    for task_id, asset in picked.items():
        try:
            title = extract_metadata_title(asset_service.read_storage_bytes(store, asset.storage_key))
        except Exception:
            continue
        if title:
            title_map[task_id] = title
    return title_map


def create_task(payload: TaskCreate, *, db: Session) -> Task:
    task = Task(
        source_type=payload.source_type,
        source_url=payload.source_url,
        source_license=payload.source_license,
        source_proof_url=payload.source_proof_url,
        priority=payload.priority,
        created_by=payload.created_by,
        status=TaskStatus.created,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def stop_task(task_id: uuid.UUID, *, db: Session) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status == TaskStatus.canceled and task.stopped_status is not None:
        return task
    if task.status not in STOPPABLE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail=f"task status={task.status.value.lower()} cannot be stopped")

    task.stopped_status = task.status
    task.status = TaskStatus.canceled
    # A stopped task must release its task-level queue slot immediately.  The
    # worker will safely return any in-flight job to queued at its next stop
    # check, while the queue UI/scheduler no longer treats this task as active.
    task.lock_owner = None
    task.lock_until = None
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def resume_stopped_task(task_id: uuid.UUID, *, db: Session) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != TaskStatus.canceled or task.stopped_status is None:
        raise HTTPException(status_code=409, detail="task was not stopped by the task controls")

    task.status = task.stopped_status
    task.stopped_status = None
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def stop_all_tasks(*, db: Session) -> tuple[int, int]:
    tasks = db.query(Task).filter(Task.status.in_(STOPPABLE_TASK_STATUSES)).all()

    # A failed worker can leave a recoverable job behind.  These tasks are
    # normally terminal and therefore are not in STOPPABLE_TASK_STATUSES, but
    # they still need to be included in a bulk stop so the queue is truly
    # drained.  Keep failed tasks with no live jobs untouched in history.
    failed_task_ids = {
        task_id
        for task_id, in db.query(SubtitleJob.task_id)
        .join(Task, Task.id == SubtitleJob.task_id)
        .filter(
            Task.status == TaskStatus.failed,
            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
        )
        .distinct()
        .all()
    }
    failed_task_ids.update(
        task_id
        for task_id, in db.query(RenderJob.task_id)
        .join(Task, Task.id == RenderJob.task_id)
        .filter(
            Task.status == TaskStatus.failed,
            RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
        )
        .distinct()
        .all()
    )
    if failed_task_ids:
        tasks.extend(db.query(Task).filter(Task.id.in_(failed_task_ids)).all())

    for task in tasks:
        task.stopped_status = task.status
        task.status = TaskStatus.canceled
        task.lock_owner = None
        task.lock_until = None
        db.add(task)

    # Also repair stale queue locks on tasks that were already stopped by a
    # previous request.  They are not counted as changed tasks, but must never
    # reserve a concurrency slot.
    db.query(Task).filter(Task.status == TaskStatus.canceled).update(
        {"lock_owner": None, "lock_until": None}, synchronize_session=False
    )
    db.commit()
    return len(tasks), len(tasks)


def resume_all_stopped_tasks(*, db: Session) -> tuple[int, int, list[Task]]:
    tasks = (
        db.query(Task)
        .filter(Task.status == TaskStatus.canceled, Task.stopped_status.is_not(None))
        .order_by(Task.updated_at.asc(), Task.created_at.asc())
        .all()
    )
    for task in tasks:
        task.status = task.stopped_status
        task.stopped_status = None
        db.add(task)
    db.commit()
    for task in tasks:
        db.refresh(task)
    return len(tasks), len(tasks), tasks


def auto_youtube_restart_options(task: Task, *, db: Session) -> tuple[bool, bool | None]:
    """Return the auto-publish option when a fully stopped pipeline must restart.

    A pipeline that still owns a live task lock will observe the restored state
    itself, so sending another Celery task would duplicate work.
    """
    if task.source_type.value != "youtube" or task.status not in {TaskStatus.ingested, TaskStatus.downloaded}:
        return False, None
    marker = parse_auto_youtube_created_by(task.created_by)
    if marker is None:
        return False, None
    now = datetime.now(timezone.utc)
    if task.lock_owner and task.lock_until and task.lock_until > now:
        return False, None
    subtitle_active = (
        db.query(SubtitleJob)
        .filter(SubtitleJob.task_id == task.id, SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]))
        .count()
    )
    render_active = (
        db.query(RenderJob)
        .filter(RenderJob.task_id == task.id, RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]))
        .count()
    )
    if subtitle_active or render_active:
        return False, None
    return True, marker.get("auto_publish")


def active_bilibili_uploads(db: Session, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Return the one live Bilibili video transfer for each task, if any."""
    if not task_ids:
        return {}
    jobs = (
        db.query(PublishJob)
        .filter(
            PublishJob.task_id.in_(task_ids),
            PublishJob.platform == Platform.bilibili,
            PublishJob.state == PublishState.submitting,
            PublishJob.upload_active.is_(True),
        )
        .order_by(PublishJob.updated_at.desc())
        .all()
    )
    uploads: dict[uuid.UUID, dict[str, Any]] = {}
    for job in jobs:
        if job.task_id in uploads:
            continue
        uploads[job.task_id] = {
            "job_id": job.id,
            "progress": max(0, min(100, int(job.upload_progress or 0))),
        }
    return uploads


def list_converted_videos(*, limit: int, db: Session) -> list[dict[str, Any]]:
    fetch_n = min(1000, max(limit, 1) * 5)
    assets = (
        db.query(Asset)
        .filter(Asset.kind == AssetKind.video_final)
        .order_by(Asset.created_at.desc())
        .limit(fetch_n)
        .all()
    )
    final_assets: list[Asset] = []
    seen: set[uuid.UUID] = set()
    for asset in assets:
        if asset.task_id in seen:
            continue
        seen.add(asset.task_id)
        final_assets.append(asset)
        if len(final_assets) >= limit:
            break
    task_ids = [asset.task_id for asset in final_assets]
    if not task_ids:
        return []
    task_map = {task.id: task for task in db.query(Task).filter(Task.id.in_(task_ids)).all()}
    cover_assets = (
        db.query(Asset)
        .filter(Asset.task_id.in_(task_ids), Asset.kind == AssetKind.cover_image)
        .order_by(Asset.created_at.desc())
        .all()
    )
    cover_by_task: dict[uuid.UUID, Asset] = {}
    for asset in cover_assets:
        cover_by_task.setdefault(asset.task_id, asset)
    title_map = load_task_display_titles(db, task_ids, allow_storage_fallback=False)
    return [
        {
            "task": task,
            "final_asset": asset,
            "cover_asset": cover_by_task.get(asset.task_id),
            "display_title": str(title_map.get(asset.task_id) or "").strip() or None,
        }
        for asset in final_assets
        if (task := task_map.get(asset.task_id)) is not None
    ]


def list_tasks(
    *,
    status: TaskStatus | None,
    limit: int,
    db: Session,
    store: FileStore,
) -> list[dict[str, Any]]:
    query = db.query(Task).order_by(Task.created_at.desc())
    if status is not None:
        query = query.filter(Task.status == status)
    tasks = query.limit(limit).all()
    task_ids = [task.id for task in tasks]
    published_task_ids = publishing_service.published_publish_job_task_ids(db, task_ids)
    reconciled = False
    for task in tasks:
        reconciled = (
            publishing_service.reconcile_published_task_state(
                db,
                task,
                published_task_ids=published_task_ids,
            )
            or reconciled
        )
    if reconciled:
        db.commit()
    title_map = load_task_display_titles(db, task_ids, store=store, allow_storage_fallback=True)
    uploads_by_task = active_bilibili_uploads(db, task_ids)
    output: list[dict[str, Any]] = []
    for task in tasks:
        if status is not None and task.status != status:
            continue
        item = TaskRead.model_validate(task).model_dump()
        item["display_title"] = str(title_map.get(task.id) or "").strip() or None
        item["bilibili_upload"] = uploads_by_task.get(task.id)
        output.append(item)
    return output


def get_task(task_id: uuid.UUID, *, db: Session, store: FileStore) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if publishing_service.reconcile_published_task_state(db, task):
        db.commit()
    item = TaskRead.model_validate(task).model_dump()
    title = get_task_display_title_with_storage(db, str(task_id), store=store).strip()
    item["display_title"] = title or None
    item["bilibili_upload"] = active_bilibili_uploads(db, [task_id]).get(task_id)
    return item
