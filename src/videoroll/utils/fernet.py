from __future__ import annotations

import os
import tempfile
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
    try:
        return Fernet(key_path.read_bytes())
    except FileNotFoundError:
        pass

    # Publish a complete, private file without replacing another process's key.
    # Exclusive creation of the final path alone would expose an empty file
    # between open() and write(); linking a flushed temporary file avoids that.
    key = Fernet.generate_key()
    fd, temporary_name = tempfile.mkstemp(prefix=".fernet-", dir=key_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(key)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_path, key_path)
        except FileExistsError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)

    # Every contender caches the winner, including the process that created it.
    return Fernet(key_path.read_bytes())


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
