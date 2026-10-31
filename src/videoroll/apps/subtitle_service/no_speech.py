"""Durable no-speech outcome for automatic YouTube tasks.

Absence of credible speech is not a pipeline failure. Reuse the source video
without fabricating captions or voice; an existing render is always preferred.
"""
from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Callable

from sqlalchemy.orm import Session

from videoroll.db.models import Asset, AssetKind, SubtitleJob, SubtitleJobStatus, Task, TaskStatus

if TYPE_CHECKING:
    from videoroll.storage.filesystem import FileStore


def latest_no_speech_job(
    db: Session, task_id: uuid.UUID, *, store: FileStore | None = None,
    store_factory: Callable[[], FileStore] | None = None,
) -> SubtitleJob | None:
    """Confirm the latest subtitle job was a successful empty ASR outcome.

    Pre-2026-10-31 jobs do not carry the explicit marker. For those, confirm
    their pinned source-segments artifact is literally [] before reusing it.
    Never infer no-speech from a failed job or from missing subtitle files.
    """
    job = (
        db.query(SubtitleJob)
        .filter(SubtitleJob.task_id == task_id)
        .order_by(SubtitleJob.created_at.desc(), SubtitleJob.id.desc())
        .first()
    )
    if job is None or job.status != SubtitleJobStatus.succeeded:
        return None
    request = job.request_json if isinstance(job.request_json, dict) else {}
    artifacts = request.get("artifacts") if isinstance(request.get("artifacts"), dict) else {}
    if artifacts.get("no_speech_detected") is True:
        return job
    if artifacts.get("final_subtitle_segments_key"):
        return None
    key = str(artifacts.get("source_segments_key") or "").strip()
    if not key:
        return None
    if store is None and store_factory is not None:
        store = store_factory()
    if store is None:
        return None
    try:
        if json.loads(store.path_for(key).read_text(encoding="utf-8")) == []:
            return job
    except (OSError, ValueError, TypeError):
        pass
    return None


def ensure_no_speech_final_asset(db: Session, task: Task) -> Asset:
    """Expose a no-caption source as the final media, without transcoding it.

    This is an asset alias, not a forged rendered video or subtitle. The same
    storage key remains referenced by the original source asset so cleanup
    treats it as live. Caller owns the transaction.
    """
    if task.status in {TaskStatus.canceled, TaskStatus.publishing, TaskStatus.published}:
        raise ValueError("cannot prepare no-speech media for a stopped or publishing task")
    final = (
        db.query(Asset)
        .filter(Asset.task_id == task.id, Asset.kind == AssetKind.video_final)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if final is None:
        source = (
            db.query(Asset)
            .filter(Asset.task_id == task.id, Asset.kind == AssetKind.video_raw)
            .order_by(Asset.created_at.desc())
            .first()
        )
        if source is None:
            raise ValueError("no-speech source video asset is missing")
        final = Asset(
            task_id=task.id, kind=AssetKind.video_final,
            storage_key=source.storage_key, sha256=source.sha256,
            size_bytes=source.size_bytes, duration_ms=source.duration_ms,
        )
        db.add(final)
    task.status = TaskStatus.rendered
    task.error_code = None
    task.error_message = None
    db.add(task)
    return final
