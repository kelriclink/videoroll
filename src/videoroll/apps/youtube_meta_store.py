from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from videoroll.db.models import YouTubeVideoMeta


def _as_str(v: object) -> str:
    return str(v or "").strip()


def get_task_youtube_meta(db: Session, task_id: uuid.UUID) -> Optional[YouTubeVideoMeta]:
    return db.get(YouTubeVideoMeta, task_id)


def upsert_task_youtube_meta(
    db: Session,
    task_id: uuid.UUID,
    *,
    source_id: str | None = None,
    title: str,
    description: str,
    webpage_url: str,
    uploader: str | None = None,
    upload_date: str | None = None,
    duration: int | None = None,
) -> YouTubeVideoMeta:
    row = db.get(YouTubeVideoMeta, task_id)
    if not row:
        row = YouTubeVideoMeta(task_id=task_id)

    row.source_id = _as_str(source_id) or None
    row.title = _as_str(title)
    row.description = str(description or "")
    row.webpage_url = _as_str(webpage_url)
    row.uploader = _as_str(uploader) or None
    row.upload_date = _as_str(upload_date) or None
    row.duration = int(duration) if duration is not None else None

    db.add(row)
    return row


def delete_task_youtube_meta(db: Session, task_id: uuid.UUID) -> int:
    return db.query(YouTubeVideoMeta).filter(YouTubeVideoMeta.task_id == task_id).delete(synchronize_session=False)



def snapshot_downloaded_youtube_info(
    db: Session,
    task_id: uuid.UUID,
    info: dict[str, object],
    *,
    fallback_url: str,
) -> YouTubeVideoMeta | None:
    """Save small source metadata before the published-task cleanup removes raw JSON.

    Author-collection sync is intentionally asynchronous, so it must not depend
    on the raw yt-dlp metadata asset surviving post-publish cleanup.
    """
    if not isinstance(info, dict) or not info:
        return None
    duration: int | None = None
    try:
        raw_duration = info.get("duration")
        if raw_duration is not None:
            duration = max(0, int(float(raw_duration)))
    except (TypeError, ValueError, OverflowError):
        pass
    uploader = _as_str(info.get("uploader") or info.get("channel")) or None
    return upsert_task_youtube_meta(
        db, task_id,
        source_id=_as_str(info.get("id")) or None,
        title=_as_str(info.get("title")),
        description=str(info.get("description") or ""),
        webpage_url=_as_str(info.get("webpage_url")) or fallback_url,
        uploader=uploader,
        upload_date=_as_str(info.get("upload_date")) or None,
        duration=duration,
    )
