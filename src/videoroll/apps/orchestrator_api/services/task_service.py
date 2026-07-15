from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.schemas import TaskCreate, TaskRead
from videoroll.apps.orchestrator_api.services import asset_service, publishing_service
from videoroll.apps.subtitle_service.task_title_store import get_task_display_title_with_s3
from videoroll.db.models import AppSetting, Asset, AssetKind, Platform, PublishJob, PublishState, Task, TaskStatus
from videoroll.storage.s3 import S3Store


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
    s3: S3Store | None = None,
    allow_s3_fallback: bool,
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

    if not allow_s3_fallback or s3 is None:
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
            title = extract_metadata_title(asset_service.read_s3_bytes(s3, asset.storage_key))
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
    title_map = load_task_display_titles(db, task_ids, allow_s3_fallback=False)
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
    s3: S3Store,
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
    title_map = load_task_display_titles(db, task_ids, s3=s3, allow_s3_fallback=True)
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


def get_task(task_id: uuid.UUID, *, db: Session, s3: S3Store) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if publishing_service.reconcile_published_task_state(db, task):
        db.commit()
    item = TaskRead.model_validate(task).model_dump()
    title = get_task_display_title_with_s3(db, str(task_id), s3=s3).strip()
    item["display_title"] = title or None
    item["bilibili_upload"] = active_bilibili_uploads(db, [task_id]).get(task_id)
    return item
