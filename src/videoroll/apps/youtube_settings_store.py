from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


YOUTUBE_SETTINGS_KEY = "youtube.settings"

_MAX_PROXY_LEN = 2048


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, YOUTUBE_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=YOUTUBE_SETTINGS_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_youtube_settings(db: Session, *, default_proxy: Optional[str] = None) -> dict[str, Any]:
    row = db.get(AppSetting, YOUTUBE_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    if "proxy" in stored:
        proxy = stored.get("proxy")
    else:
        proxy = default_proxy

    proxy_str = str(proxy or "").strip()
    if len(proxy_str) > _MAX_PROXY_LEN:
        proxy_str = proxy_str[:_MAX_PROXY_LEN]

    return {"proxy": proxy_str}


def update_youtube_settings(db: Session, update: dict[str, Any], *, default_proxy: Optional[str] = None) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "proxy" in update and update["proxy"] is not None:
        proxy = str(update.get("proxy") or "").strip()
        if len(proxy) > _MAX_PROXY_LEN:
            raise ValueError(f"proxy is too long (max {_MAX_PROXY_LEN} chars)")
        stored["proxy"] = proxy

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_youtube_settings(db, default_proxy=default_proxy)

