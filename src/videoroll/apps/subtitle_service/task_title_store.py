from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting, Asset, AssetKind
from videoroll.storage.s3 import S3Store


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
    return get_task_display_title_with_s3(db, task_id, s3=None)


def get_task_display_title_with_s3(db: Session, task_id: str, *, s3: S3Store | None) -> str:
    t = get_task_titles(db, task_id)
    out = str(t.get("translated_title") or t.get("source_title") or "").strip()
    if out:
        return out

    if s3 is None:
        return ""

    try:
        tid = uuid.UUID(str(task_id))
    except Exception:
        return ""

    asset = (
        db.query(Asset)
        .filter(Asset.task_id == tid, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not asset:
        return ""

    try:
        obj = s3.get_object(asset.storage_key)
        body = obj.get("Body")
        raw = body.read() if body else b""
        try:
            if body:
                body.close()
        except Exception:
            pass
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        info = parsed if isinstance(parsed, dict) else {}
        title = str(info.get("title") or info.get("fulltitle") or info.get("alt_title") or "").strip()
        return title
    except Exception:
        return ""


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
