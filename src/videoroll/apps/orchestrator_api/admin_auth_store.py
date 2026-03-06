from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting


ADMIN_AUTH_SETTINGS_KEY = "admin.auth"

DEVICE_COOKIE_NAME = "videoroll_admin_device"
INTERNAL_TOKEN_HEADER = "X-Videoroll-Internal-Token"

_PBKDF2_ITERATIONS = 200_000
_PBKDF2_SALT_BYTES = 16

# Device cookies are "trusted device" sessions; user can always re-login with the shared admin password.
_DEVICE_COOKIE_MAX_AGE_SECONDS = 3600 * 24 * 180  # 180 days


@dataclass(frozen=True)
class AdminAuthStatus:
    password_set: bool
    trusted: bool


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, ADMIN_AUTH_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=ADMIN_AUTH_SETTINGS_KEY, value_json={})
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        # Concurrency: another request created the row first.
        db.rollback()
        row2 = db.get(AppSetting, ADMIN_AUTH_SETTINGS_KEY)
        if row2:
            return row2
        raise
    return row


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    t = str(text or "").strip()
    if not t:
        return b""
    pad = "=" * ((4 - (len(t) % 4)) % 4)
    return base64.urlsafe_b64decode((t + pad).encode("utf-8"))


def _hash_password_pbkdf2(password: str, *, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def encode_password_hash(password: str) -> str:
    """
    Returns: pbkdf2_sha256$<iterations>$<salt_b64url>$<hash_b64url>
    """
    pw = str(password or "")
    if not pw:
        raise ValueError("password is required")
    salt = secrets.token_bytes(_PBKDF2_SALT_BYTES)
    digest = _hash_password_pbkdf2(pw, salt=salt, iterations=_PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${_b64url_encode(salt)}${_b64url_encode(digest)}"


def verify_password_hash(password: str, encoded: str) -> bool:
    pw = str(password or "")
    encoded = str(encoded or "").strip()
    if not pw or not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 4:
        return False
    algo, iter_s, salt_s, digest_s = parts
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iter_s)
    except Exception:
        return False
    salt = _b64url_decode(salt_s)
    digest = _b64url_decode(digest_s)
    if not salt or not digest or iterations <= 0:
        return False
    actual = _hash_password_pbkdf2(pw, salt=salt, iterations=iterations)
    return hmac.compare_digest(actual, digest)


def get_password_hash(db: Session) -> str:
    row = db.get(AppSetting, ADMIN_AUTH_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    val = str(stored.get("password_hash") or "").strip()
    return val


def set_password_hash(db: Session, password_hash: str) -> None:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))
    stored["password_hash"] = str(password_hash or "").strip()
    row.value_json = stored
    db.add(row)
    db.commit()


def is_password_set(db: Session) -> bool:
    return bool(get_password_hash(db))


def device_cookie_max_age_seconds() -> int:
    return int(_DEVICE_COOKIE_MAX_AGE_SECONDS)


def _cookie_signing_key(*, internal_secret: str, password_hash: str) -> bytes:
    """
    Derive a stable signing key. Including password_hash allows invalidating
    existing device cookies after an admin password change.
    """
    internal_secret = str(internal_secret or "").strip()
    password_hash = str(password_hash or "").strip()
    h = hashlib.sha256()
    h.update(("videoroll-admin-cookie-key:v1:" + internal_secret + ":" + password_hash).encode("utf-8"))
    return h.digest()


def mint_device_cookie_value(*, internal_secret: str, password_hash: str, now: float | None = None) -> str:
    """
    Cookie format: <token>.<exp_unix>.<sig_b64url>
    """
    now_ts = float(time.time() if now is None else now)
    exp = int(now_ts) + device_cookie_max_age_seconds()
    token = _b64url_encode(secrets.token_bytes(32))
    msg = f"{token}.{exp}"
    key = _cookie_signing_key(internal_secret=internal_secret, password_hash=password_hash)
    sig = hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
    return f"{msg}.{_b64url_encode(sig)}"


def verify_device_cookie_value(value: str, *, internal_secret: str, password_hash: str, now: float | None = None) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    parts = raw.split(".")
    if len(parts) != 3:
        return False
    token, exp_s, sig_s = parts
    if not token or not exp_s or not sig_s:
        return False
    try:
        exp = int(exp_s)
    except Exception:
        return False
    now_ts = float(time.time() if now is None else now)
    if int(now_ts) >= exp:
        return False
    msg = f"{token}.{exp}"
    key = _cookie_signing_key(internal_secret=internal_secret, password_hash=password_hash)
    expected_sig = hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
    got_sig = _b64url_decode(sig_s)
    if not got_sig:
        return False
    return hmac.compare_digest(expected_sig, got_sig)


def validate_new_password(password: str) -> str:
    pw = str(password or "")
    if len(pw) < 8:
        raise ValueError("password too short (min 8 chars)")
    if len(pw) > 128:
        raise ValueError("password too long (max 128 chars)")
    return pw
