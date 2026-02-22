from __future__ import annotations

from functools import lru_cache
from pathlib import Path


def _secret_dir_candidates() -> list[Path]:
    # Prefer a docker volume mount: ./data/secrets:/secrets
    return [
        Path("/secrets"),
        Path(".") / "data" / "secrets",
        Path.home() / ".videoroll" / "secrets",
    ]


def _ensure_writable_dir() -> Path:
    last_err: Exception | None = None
    for d in _secret_dir_candidates():
        try:
            d.mkdir(parents=True, exist_ok=True)
            test = d / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
            return d
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"no writable secret dir found (last_err={last_err})")


def _key_path() -> Path:
    return _ensure_writable_dir() / "fernet.key"


@lru_cache
def _fernet():
    try:
        from cryptography.fernet import Fernet  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("cryptography is not installed") from e

    key_path = _key_path()
    if key_path.exists():
        key = key_path.read_bytes()
        if key:
            return Fernet(key)

    key = Fernet.generate_key()
    key_path.write_bytes(key)
    return Fernet(key)


def encrypt_str(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    f = _fernet()
    return f.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_str(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    f = _fernet()
    return f.decrypt(token.encode("utf-8")).decode("utf-8")

