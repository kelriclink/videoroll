from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


STORAGE_RETENTION_SETTINGS_KEY = "storage.retention"

_MAX_TTL_DAYS = 3650


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, STORAGE_RETENTION_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=STORAGE_RETENTION_SETTINGS_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_storage_retention_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, STORAGE_RETENTION_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    ttl_days = stored.get("asset_ttl_days")
    try:
        ttl_days_int = int(ttl_days) if ttl_days is not None else 0
    except Exception:
        ttl_days_int = 0
    ttl_days_int = max(0, min(_MAX_TTL_DAYS, ttl_days_int))
    return {"asset_ttl_days": ttl_days_int}


def update_storage_retention_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "asset_ttl_days" in update and update["asset_ttl_days"] is not None:
        try:
            ttl_days = int(update["asset_ttl_days"])
        except Exception as e:
            raise ValueError("asset_ttl_days must be an integer") from e
        ttl_days = max(0, min(_MAX_TTL_DAYS, ttl_days))
        stored["asset_ttl_days"] = ttl_days

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_storage_retention_settings(db)
