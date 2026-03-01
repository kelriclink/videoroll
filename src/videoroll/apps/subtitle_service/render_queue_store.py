from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


RENDER_QUEUE_SETTINGS_KEY = "subtitle.render_queue"

_MAX_CONCURRENCY_MIN = 0
_MAX_CONCURRENCY_MAX = 32


def _default_settings() -> dict[str, Any]:
    return {"max_concurrency": 1}


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, RENDER_QUEUE_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=RENDER_QUEUE_SETTINGS_KEY, value_json={})
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
    row = db.get(AppSetting, RENDER_QUEUE_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    baseline = _default_settings()
    merged = {**baseline, **stored}
    merged["max_concurrency"] = _normalize_max_concurrency(merged.get("max_concurrency"))
    return merged


def update_render_queue_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "max_concurrency" in update and update["max_concurrency"] is not None:
        stored["max_concurrency"] = _normalize_max_concurrency(update["max_concurrency"])

    row.value_json = stored
    db.add(row)
    db.commit()
    return get_render_queue_settings(db)

