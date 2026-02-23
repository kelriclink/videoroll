from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _key(task_id: str) -> str:
    return f"task.title.{task_id}"


def get_task_titles(db: Session, task_id: str) -> dict[str, str]:
    row = db.get(AppSetting, _key(task_id))
    if not row:
        return {}
    data = _as_dict(row.value_json)
    out: dict[str, str] = {}
    for k in ["source_title", "translated_title"]:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def get_task_display_title(db: Session, task_id: str) -> str:
    t = get_task_titles(db, task_id)
    return str(t.get("translated_title") or t.get("source_title") or "").strip()


def set_task_titles(
    db: Session,
    task_id: str,
    *,
    source_title: str | None = None,
    translated_title: str | None = None,
) -> None:
    row = db.get(AppSetting, _key(task_id))
    if not row:
        row = AppSetting(key=_key(task_id), value_json={})
        db.add(row)

    data = dict(_as_dict(row.value_json))
    if source_title is not None:
        data["source_title"] = str(source_title or "").strip()
    if translated_title is not None:
        data["translated_title"] = str(translated_title or "").strip()
    row.value_json = data
    db.add(row)
    db.commit()

