from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


#
# NOTE: This module was originally used for the "render queue" (ffmpeg only).
# The queue is now task-based: max_concurrency limits how many *tasks* (pipelines)
# can be in-flight at the same time.
#
# We keep the filename and legacy key for backward compatibility.
#
LEGACY_RENDER_QUEUE_SETTINGS_KEY = "subtitle.render_queue"
TASK_QUEUE_SETTINGS_KEY = "subtitle.task_queue"

_MAX_CONCURRENCY_MIN = 0
_MAX_CONCURRENCY_MAX = 32


def _default_settings() -> dict[str, Any]:
    return {"max_concurrency": 1}


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session, key: str) -> AppSetting:
    row = db.get(AppSetting, key)
    if row:
        return row
    row = AppSetting(key=key, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _normalize_max_concurrency(v: Any) -> int:
    try:
        n = int(v)
    except Exception:
        n = _default_settings()["max_concurrency"]
    if n < _MAX_CONCURRENCY_MIN:
        n = _MAX_CONCURRENCY_MIN
    if n > _MAX_CONCURRENCY_MAX:
        n = _MAX_CONCURRENCY_MAX
    return n


def get_render_queue_settings(db: Session) -> dict[str, Any]:
    # Legacy alias.
    return get_task_queue_settings(db)


def get_task_queue_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, TASK_QUEUE_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    # Auto-migrate legacy key on first read (best-effort).
    if not stored:
        legacy = db.get(AppSetting, LEGACY_RENDER_QUEUE_SETTINGS_KEY)
        legacy_stored = dict(_as_dict(legacy.value_json)) if legacy else {}
        if legacy_stored:
            stored = legacy_stored
            try:
                new_row = _get_row(db, TASK_QUEUE_SETTINGS_KEY)
                new_row.value_json = dict(legacy_stored)
                db.add(new_row)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

    baseline = _default_settings()
    merged = {**baseline, **stored}
    merged["max_concurrency"] = _normalize_max_concurrency(merged.get("max_concurrency"))
    return merged


def update_render_queue_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    # Legacy alias.
    return update_task_queue_settings(db, update)


def update_task_queue_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db, TASK_QUEUE_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json))

    if "max_concurrency" in update and update["max_concurrency"] is not None:
        stored["max_concurrency"] = _normalize_max_concurrency(update["max_concurrency"])

    row.value_json = stored
    db.add(row)
    db.commit()
    return get_task_queue_settings(db)
