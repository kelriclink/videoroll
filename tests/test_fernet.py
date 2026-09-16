from __future__ import annotations

import multiprocessing
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from videoroll.utils import fernet as key_store


@pytest.fixture
def key_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fernet.key"
    monkeypatch.setattr(key_store, "_key_path", lambda: path)
    key_store._fernet.cache_clear()
    yield path
    key_store._fernet.cache_clear()


def _encrypt_on_first_use(key_path: str, barrier, results) -> None:
    key_store._fernet.cache_clear()
    original_generate = Fernet.generate_key

    def simultaneous_generate() -> bytes:
        barrier.wait(timeout=15)
        return original_generate()

    with (
        patch.object(key_store, "_key_path", return_value=Path(key_path)),
        patch.object(Fernet, "generate_key", side_effect=simultaneous_generate),
    ):
        results.put(key_store.encrypt_str("offline-test-setting"))


def test_concurrent_first_use_keeps_every_process_setting_decryptable(key_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [context.Process(target=_encrypt_on_first_use, args=(str(key_path), barrier, results)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        encrypted = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0

        # A fresh process has only the persisted key, not either worker's cache.
        persisted = Fernet(key_path.read_bytes())
        assert [persisted.decrypt(value.encode()) for value in encrypted] == [
            b"offline-test-setting",
            b"offline-test-setting",
        ]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
        results.close()


def test_existing_key_and_settings_survive_cache_reset(key_path: Path) -> None:
    original_key = Fernet.generate_key()
    key_path.write_bytes(original_key)
    encrypted = key_store.encrypt_str("saved-setting")
    key_store._fernet.cache_clear()

    assert key_store.decrypt_str(encrypted) == "saved-setting"
    assert key_path.read_bytes() == original_key


@pytest.mark.parametrize("invalid_key", [b"", b"invalid-existing-key"])
def test_invalid_existing_key_is_not_silently_replaced(key_path: Path, invalid_key: bytes) -> None:
    key_path.write_bytes(invalid_key)

    with pytest.raises(ValueError):
        key_store.encrypt_str("new-setting")

    assert key_path.read_bytes() == invalid_key


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_new_key_is_private_to_the_service_user(key_path: Path) -> None:
    key_store.encrypt_str("new-setting")

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
