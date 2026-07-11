from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from videoroll.apps.publish_gateway import normalize_publish_platform
from videoroll.apps.social_publisher.account_store import (
    canonicalize_storage_state,
    decrypt_account_state,
    validate_account_name,
)
from videoroll.config import SocialPublisherSettings
from videoroll.db.models import Account
from videoroll.utils.fernet import encrypt_str


def social_lock_key(platform: str, account_id: uuid.UUID) -> str:
    return f"videoroll:social-publish:{normalize_publish_platform(platform)}:{account_id}"


@contextmanager
def materialized_account_state(account: Account, settings: SocialPublisherSettings) -> Iterator[Path]:
    cookies_dir = Path(settings.sau_cookies_dir).resolve()
    cookies_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{account.platform.value}_{validate_account_name(account.name)}.json"
    path = (cookies_dir / filename).resolve()
    if path.parent != cookies_dir:
        raise ValueError("unsafe SAU account path")
    path.write_text(decrypt_account_state(account), encoding="utf-8")
    path.chmod(0o600)
    try:
        yield path
    finally:
        if path.exists():
            try:
                canonical = canonicalize_storage_state(path.read_bytes())
                account.secrets_encrypted = encrypt_str(canonical)
            finally:
                path.unlink(missing_ok=True)
