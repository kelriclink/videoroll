from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.apps.publish_gateway import SUPPORTED_PUBLISH_PLATFORMS, normalize_publish_platform
from videoroll.db.models import AppSetting


PUBLISH_PLATFORM_SETTINGS_KEY = "publish.platforms"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _defaults() -> dict[str, bool]:
    return {platform: False for platform in sorted(SUPPORTED_PUBLISH_PLATFORMS)}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, PUBLISH_PLATFORM_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=PUBLISH_PLATFORM_SETTINGS_KEY, value_json={})
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        existing = db.get(AppSetting, PUBLISH_PLATFORM_SETTINGS_KEY)
        if existing:
            return existing
        raise
    return row


def get_publish_platform_settings(db: Session) -> dict[str, bool]:
    row = db.get(AppSetting, PUBLISH_PLATFORM_SETTINGS_KEY)
    stored = _as_dict(row.value_json) if row else {}
    return {platform: bool(stored.get(platform, False)) for platform in _defaults()}


def update_publish_platform_setting(db: Session, platform: object, enabled: bool) -> dict[str, bool]:
    value = normalize_publish_platform(platform)
    row = _get_row(db)
    stored = _as_dict(row.value_json)
    row.value_json = {**stored, value: bool(enabled)}
    db.add(row)
    db.commit()
    return get_publish_platform_settings(db)


def is_publish_platform_enabled(db: Session, platform: object) -> bool:
    value = normalize_publish_platform(platform)
    return get_publish_platform_settings(db)[value]
