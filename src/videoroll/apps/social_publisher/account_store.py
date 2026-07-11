from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from videoroll.apps.publish_gateway import normalize_publish_platform
from videoroll.apps.social_publisher.schemas import SocialAccountRead
from videoroll.db.models import Account, Platform
from videoroll.utils.fernet import decrypt_str, encrypt_str


ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_STORAGE_STATE_BYTES = 1024 * 1024
SOCIAL_PLATFORMS = {Platform.douyin, Platform.xiaohongshu, Platform.kuaishou}


def validate_account_name(name: str) -> str:
    value = str(name or "").strip()
    if not ACCOUNT_NAME_RE.fullmatch(value):
        raise ValueError("invalid account name")
    return value


def canonicalize_storage_state(data: bytes) -> str:
    if len(data) > MAX_STORAGE_STATE_BYTES:
        raise ValueError("storage_state exceeds 1 MiB")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("storage_state must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("storage_state root must be an object")
    if not isinstance(parsed.get("cookies"), list):
        raise ValueError("storage_state must contain a cookies array")
    origins = parsed.get("origins", [])
    if not isinstance(origins, list):
        raise ValueError("storage_state origins must be an array")
    parsed["origins"] = origins
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def account_read(account: Account) -> SocialAccountRead:
    return SocialAccountRead(
        id=account.id,
        platform=str(getattr(account.platform, "value", account.platform)),
        name=account.name,
        is_active=bool(account.is_active),
        check_state=str(account.check_state or "unchecked"),
        last_checked_at=account.last_checked_at,
        last_check_message=account.last_check_message,
        rotated_at=account.rotated_at,
    )


def upsert_account(db: Session, platform: str, name: str, canonical_json: str) -> Account:
    platform_enum = Platform(normalize_publish_platform(platform))
    if platform_enum not in SOCIAL_PLATFORMS:
        raise ValueError("unsupported social account platform")
    safe_name = validate_account_name(name)
    account = (
        db.query(Account)
        .filter(Account.platform == platform_enum, Account.name == safe_name)
        .one_or_none()
    )
    if account is None:
        account = Account(platform=platform_enum, name=safe_name)
    account.secrets_encrypted = encrypt_str(canonical_json)
    account.rotated_at = datetime.now(timezone.utc)
    account.is_active = True
    account.check_state = "queued"
    account.last_checked_at = None
    account.last_check_message = None
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def decrypt_account_state(account: Account) -> str:
    value = decrypt_str(account.secrets_encrypted)
    if not value:
        raise ValueError("account storage_state is missing")
    canonicalize_storage_state(value.encode("utf-8"))
    return value


def disable_account(db: Session, account: Account) -> None:
    account.is_active = False
    account.secrets_encrypted = ""
    account.check_state = "unchecked"
    account.last_check_message = None
    db.add(account)
    db.commit()
