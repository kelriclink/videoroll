from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta
from videoroll.db.models import AppSetting


BILIBILI_PUBLISH_SETTINGS_KEY = "bilibili.publish"


def _default_meta() -> dict[str, Any]:
    return {
        "title": "示例标题",
        "desc": "示例简介（请包含来源/授权说明）",
        "tags": ["videoroll"],
        "typeid": 17,
        "copyright": 1,
        "source": "",
        "dtime": None,
        "dynamic": "",
        "recreate": -1,
        "no_reprint": 1,
        "no_disturbance": 0,
        "subtitle": {"open": 0, "lan": ""},
        "up_selection_reply": False,
        "up_close_reply": False,
        "up_close_danmu": False,
        "web_os": 3,
    }


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, BILIBILI_PUBLISH_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=BILIBILI_PUBLISH_SETTINGS_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_bilibili_publish_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, BILIBILI_PUBLISH_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    baseline = _default_meta()
    meta_update = _as_dict(stored.get("default_meta"))
    merged = {**baseline, **meta_update}

    try:
        meta = BilibiliPublishMeta.model_validate(merged)
    except Exception:
        meta = BilibiliPublishMeta.model_validate(baseline)

    return {"default_meta": meta}


def update_bilibili_publish_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "default_meta" in update and update["default_meta"] is not None:
        meta_in = update["default_meta"]
        if not isinstance(meta_in, dict):
            raise ValueError("default_meta must be an object")

        baseline = _default_meta()
        merged = {**baseline, **meta_in}
        meta = BilibiliPublishMeta.model_validate(merged)
        stored["default_meta"] = meta.model_dump()

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_bilibili_publish_settings(db)

