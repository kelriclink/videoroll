from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.admin_auth_store import encode_password_hash, verify_password_hash
from videoroll.db.models import AppSetting


REMOTE_API_SETTINGS_KEY = "remote.api"

REMOTE_AUTO_YOUTUBE_PATH = "/remote/auto/youtube"
REMOTE_API_TOKEN_QUERY_PARAM = "token"
REMOTE_AUTO_URL_QUERY_PARAM = "url"
REMOTE_AUTO_LICENSE_QUERY_PARAM = "license"
REMOTE_AUTO_PROOF_URL_QUERY_PARAM = "proof_url"

_MIN_TOKEN_LEN = 8
_MAX_TOKEN_LEN = 512


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, REMOTE_API_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=REMOTE_API_SETTINGS_KEY, value_json={})
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        row2 = db.get(AppSetting, REMOTE_API_SETTINGS_KEY)
        if row2:
            return row2
        raise
    return row


def _normalize_token(token: Any) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    if len(value) < _MIN_TOKEN_LEN:
        raise ValueError(f"token too short (min {_MIN_TOKEN_LEN} chars)")
    if len(value) > _MAX_TOKEN_LEN:
        raise ValueError(f"token too long (max {_MAX_TOKEN_LEN} chars)")
    return value


def get_remote_api_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, REMOTE_API_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    token_hash = str(stored.get("token_hash") or "").strip()
    token_updated_at = str(stored.get("token_updated_at") or "").strip() or None
    return {
        "token_set": bool(token_hash),
        "token_updated_at": token_updated_at,
        "endpoint_path": REMOTE_AUTO_YOUTUBE_PATH,
        "token_query_param": REMOTE_API_TOKEN_QUERY_PARAM,
        "url_query_param": REMOTE_AUTO_URL_QUERY_PARAM,
        "license_query_param": REMOTE_AUTO_LICENSE_QUERY_PARAM,
        "proof_url_query_param": REMOTE_AUTO_PROOF_URL_QUERY_PARAM,
    }


def update_remote_api_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "token" in update:
        token_raw = update.get("token")
        if token_raw is not None:
            token = _normalize_token(token_raw)
            if token:
                stored["token_hash"] = encode_password_hash(token)
                stored["token_updated_at"] = datetime.now(timezone.utc).isoformat()
            else:
                stored.pop("token_hash", None)
                stored.pop("token_updated_at", None)

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_remote_api_settings(db)


def remote_api_token_is_configured(db: Session) -> bool:
    row = db.get(AppSetting, REMOTE_API_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    return bool(str(stored.get("token_hash") or "").strip())


def verify_remote_api_token(db: Session, token: str) -> bool:
    value = str(token or "").strip()
    if not value:
        return False
    row = db.get(AppSetting, REMOTE_API_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    token_hash = str(stored.get("token_hash") or "").strip()
    if not token_hash:
        return False
    return verify_password_hash(value, token_hash)
