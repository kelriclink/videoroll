from __future__ import annotations

import os
from pathlib import Path


# Historical module name kept to avoid a broad compatibility rename.
# VideoRoll no longer uses a filesystem Fernet key. Credentials are intentionally
# stored in PostgreSQL in recoverable plaintext because the deployment model
# treats the database as the only durable state that must survive a rebuild.
_DB_PLAINTEXT_PREFIX = "dbplain:v1:"
_LEGACY_FERNET_PREFIX = "gAAAA"


class LegacyEncryptedSecretError(ValueError):
    """Raised when a pre-DB-only Fernet ciphertext cannot be recovered."""


def is_legacy_encrypted_token(value: object) -> bool:
    token = str(value or "").strip()
    return token.startswith(_LEGACY_FERNET_PREFIX)


def encrypt_str(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return f"{_DB_PLAINTEXT_PREFIX}{value}"


def _legacy_key_path() -> Path | None:
    """Resolve a retained pre-migration credential key without guessing paths."""
    explicit = str(os.getenv("VIDEOROLL_FERNET_KEY_PATH") or "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            raise LegacyEncryptedSecretError("VIDEOROLL_FERNET_KEY_PATH must be absolute")
        return path
    data_root = str(os.getenv("VIDEOROLL_DATA_ROOT") or "").strip()
    if data_root:
        root = Path(data_root)
        if not root.is_absolute() or root == Path("/"):
            raise LegacyEncryptedSecretError("Invalid VIDEOROLL_DATA_ROOT for secret recovery")
        return root / "secrets" / "fernet.key"
    return None


def decrypt_str(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if token.startswith(_DB_PLAINTEXT_PREFIX):
        return token[len(_DB_PLAINTEXT_PREFIX) :]
    if is_legacy_encrypted_token(token):
        path = _legacy_key_path()
        if path is None or not path.is_file():
            raise LegacyEncryptedSecretError(
                "Legacy Fernet credential needs the retained data/secrets/fernet.key"
            )
        try:
            from cryptography.fernet import Fernet, InvalidToken

            return Fernet(path.read_bytes().strip()).decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise LegacyEncryptedSecretError("Legacy Fernet key could not decrypt this credential") from exc
    # Accept unprefixed database plaintext for hand-edited/legacy rows.
    return token
