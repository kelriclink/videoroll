from __future__ import annotations

import pytest

from videoroll.utils import fernet as secret_store


def test_database_plaintext_round_trip_needs_no_external_key() -> None:
    stored = secret_store.encrypt_str("saved-setting")

    assert stored == "dbplain:v1:saved-setting"
    assert secret_store.decrypt_str(stored) == "saved-setting"


def test_empty_values_remain_empty() -> None:
    assert secret_store.encrypt_str("") == ""
    assert secret_store.decrypt_str("") == ""


def test_unprefixed_database_plaintext_is_accepted() -> None:
    assert secret_store.decrypt_str("manually-stored-secret") == "manually-stored-secret"


def test_legacy_fernet_ciphertext_is_not_mistaken_for_a_real_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VIDEOROLL_DATA_ROOT", str(tmp_path))
    legacy = "gAAAAABlegacy-ciphertext"

    assert secret_store.is_legacy_encrypted_token(legacy) is True
    with pytest.raises(secret_store.LegacyEncryptedSecretError):
        secret_store.decrypt_str(legacy)


def test_database_plaintext_prefix_is_not_classified_as_legacy() -> None:
    assert secret_store.is_legacy_encrypted_token("dbplain:v1:gAAAA-not-fernet") is False


def test_retained_fernet_key_restores_legacy_credentials(monkeypatch, tmp_path) -> None:
    from cryptography.fernet import Fernet

    data = tmp_path / "persisted"
    directory = data / "secrets"
    directory.mkdir(parents=True)
    key = Fernet.generate_key()
    (directory / "fernet.key").write_bytes(key + b"\n")
    monkeypatch.setenv("VIDEOROLL_DATA_ROOT", str(data))
    token = Fernet(key).encrypt(b"previous-provider-credential").decode("ascii")

    assert secret_store.decrypt_str(token) == "previous-provider-credential"


def test_wrong_old_fernet_key_is_not_silently_accepted(monkeypatch, tmp_path) -> None:
    from cryptography.fernet import Fernet

    data = tmp_path / "persisted"
    directory = data / "secrets"
    directory.mkdir(parents=True)
    (directory / "fernet.key").write_bytes(Fernet.generate_key())
    monkeypatch.setenv("VIDEOROLL_DATA_ROOT", str(data))
    token = Fernet(Fernet.generate_key()).encrypt(b"must-not-appear").decode()
    with pytest.raises(secret_store.LegacyEncryptedSecretError):
        secret_store.decrypt_str(token)
