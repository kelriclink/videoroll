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

