from __future__ import annotations


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


def decrypt_str(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if token.startswith(_DB_PLAINTEXT_PREFIX):
        return token[len(_DB_PLAINTEXT_PREFIX) :]
    if is_legacy_encrypted_token(token):
        raise LegacyEncryptedSecretError(
            "legacy Fernet ciphertext requires the retired external fernet.key; "
            "re-enter this credential or run DB-only recovery to clear it"
        )
    # Accept unprefixed database plaintext for hand-edited/legacy rows.
    return token
