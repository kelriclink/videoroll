from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "native" / "preflight.py"
spec = importlib.util.spec_from_file_location("videoroll_native_preflight", SCRIPT)
assert spec is not None and spec.loader is not None
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


@pytest.fixture
def native_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/videoroll",
        "HATCHET_DATABASE_URL": "postgresql://u:p@localhost:5432/videoroll_hatchet",
        "INTERNAL_API_SECRET": "test_internal_secret_1234567890",
        "ADMIN_BOOTSTRAP_SECRET": "test_bootstrap_secret_1234567890",
        "HATCHET_ADMIN_PASSWORD": "hatchet_test_pass_1234567890",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_native_preflight_accepts_distinct_databases(native_env: None) -> None:
    preflight.check(connect=False)


@pytest.mark.parametrize("origin,cookie,insecure,message", [
    ("http://localhost:3000", "localhost", "true", "old Compose"),
    ("https://media.example.org", "localhost", "false", "HATCHET_COOKIE_DOMAIN"),
    ("https://media.example.org", "media.example.org", "true", "HATCHET_COOKIE_INSECURE"),
])
def test_preflight_rejects_unsafe_public_hatchet_login(native_env, monkeypatch, origin, cookie, insecure, message):
    monkeypatch.setenv("HATCHET_SERVER_URL", origin)
    monkeypatch.setenv("HATCHET_COOKIE_DOMAIN", cookie)
    monkeypatch.setenv("HATCHET_COOKIE_INSECURE", insecure)
    with pytest.raises(ValueError, match=message):
        preflight.check(connect=False)


def test_preflight_accepts_valid_https_hatchet_origin(native_env, monkeypatch):
    monkeypatch.setenv("HATCHET_SERVER_URL", "https://media.example.org")
    monkeypatch.setenv("HATCHET_FRONTEND_URL", "https://media.example.org/workflow-ui/")
    monkeypatch.setenv("HATCHET_COOKIE_DOMAIN", "media.example.org")
    monkeypatch.setenv("HATCHET_COOKIE_INSECURE", "false")
    preflight.check(connect=False)


def test_native_preflight_rejects_identical_database(
    native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCHET_DATABASE_URL", "postgresql://u:p@localhost:5432/videoroll")
    with pytest.raises(ValueError, match="different"):
        preflight.check(connect=False)


def test_native_preflight_rejects_template_credentials(
    native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCHET_ADMIN_PASSWORD", "CHANGE_ME")
    with pytest.raises(ValueError, match="HATCHET_ADMIN_PASSWORD"):
        preflight.check(connect=False)


def test_native_preflight_requires_postgresql(
    native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCHET_DATABASE_URL", "sqlite:///tmp/runtime.db")
    with pytest.raises(ValueError, match="PostgreSQL"):
        preflight.check(connect=False)


def test_same_database_with_different_role_and_default_port_is_rejected(native_env, monkeypatch) -> None:
    monkeypatch.setenv("HATCHET_DATABASE_URL", "postgresql://other:p@127.0.0.1/videoroll")
    with pytest.raises(ValueError, match="different"):
        preflight.check(connect=False)
