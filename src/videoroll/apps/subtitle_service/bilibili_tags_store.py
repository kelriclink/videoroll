from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _key(task_id: str) -> str:
    return f"bilibili.tags.{task_id}"


def get_task_bilibili_tags(db: Session, task_id: str) -> list[str]:
    row = db.get(AppSetting, _key(task_id))
    if not row:
        return []
    data = _as_dict(row.value_json)
    tags = data.get("tags")
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        s = str(t or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def get_task_bilibili_summary(db: Session, task_id: str) -> str:
    row = db.get(AppSetting, _key(task_id))
    if not row:
        return ""
    data = _as_dict(row.value_json)
    return str(data.get("summary") or "").strip()


def set_task_bilibili_tags(
    db: Session,
    task_id: str,
    *,
    tags: list[str],
    title: str | None = None,
    summary: str | None = None,
) -> None:
    clean: list[str] = []
    seen: set[str] = set()
    for t in tags:
        s = str(t or "").strip().lstrip("#").lstrip("＃")
        s = "".join(s.split())
        if not s:
            continue
        if len(s) > 20:
            s = s[:20]
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        clean.append(s)

    row = db.get(AppSetting, _key(task_id))
    if not row:
        row = AppSetting(key=_key(task_id), value_json={})
        db.add(row)

    payload: dict[str, Any] = {"tags": clean}
    if title is not None:
        payload["title"] = str(title or "").strip()
    if summary is not None:
        payload["summary"] = str(summary or "").strip()
    row.value_json = payload
    db.add(row)
    db.commit()
