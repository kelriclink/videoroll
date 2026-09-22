from __future__ import annotations

from videoroll.db import auto_migrate as auto_migrate_module
from videoroll.db import session as session_module


def test_get_engine_isolated_per_pid(monkeypatch) -> None:
    session_module._get_engine_cached.cache_clear()
    calls: list[tuple[str, bool, dict[str, object]]] = []

    def fake_create_engine(url: str, *, pool_pre_ping: bool, connect_args: dict[str, object]):
        engine = object()
        calls.append((url, pool_pre_ping, connect_args))
        return engine

    monkeypatch.setattr(session_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(session_module.os, "getpid", lambda: 1001)

    first = session_module.get_engine("postgresql+psycopg://user:pass@db/app")
    second = session_module.get_engine("postgresql+psycopg://user:pass@db/app")

    assert first is second
    assert len(calls) == 1

    monkeypatch.setattr(session_module.os, "getpid", lambda: 1002)
    third = session_module.get_engine("postgresql+psycopg://user:pass@db/app")

    assert third is not first
    assert len(calls) == 2


def test_auto_migrate_cached_per_pid(monkeypatch) -> None:
    auto_migrate_module._auto_migrate_cached.cache_clear()
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(auto_migrate_module, "get_engine", lambda url: f"engine-for-{url}")
    monkeypatch.setattr(
        auto_migrate_module,
        "auto_migrate_engine",
        lambda engine: calls.append(("auto_migrate_engine", engine)),
    )
    monkeypatch.setattr(auto_migrate_module.os, "getpid", lambda: 2001)

    auto_migrate_module.auto_migrate("postgresql+psycopg://user:pass@db/app")
    auto_migrate_module.auto_migrate("postgresql+psycopg://user:pass@db/app")

    assert calls == [("auto_migrate_engine", "engine-for-postgresql+psycopg://user:pass@db/app")]

    monkeypatch.setattr(auto_migrate_module.os, "getpid", lambda: 2002)
    auto_migrate_module.auto_migrate("postgresql+psycopg://user:pass@db/app")

    assert calls == [
        ("auto_migrate_engine", "engine-for-postgresql+psycopg://user:pass@db/app"),
        ("auto_migrate_engine", "engine-for-postgresql+psycopg://user:pass@db/app"),
    ]


def test_auto_migrate_force_bypasses_process_cache(monkeypatch) -> None:
    auto_migrate_module._auto_migrate_cached.cache_clear()
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(auto_migrate_module, "get_engine", lambda url: f"engine-for-{url}")
    monkeypatch.setattr(
        auto_migrate_module,
        "auto_migrate_engine",
        lambda engine: calls.append(("auto_migrate_engine", engine)),
    )
    monkeypatch.setattr(auto_migrate_module.os, "getpid", lambda: 3001)

    auto_migrate_module.auto_migrate("postgresql+psycopg://user:pass@db/app")
    auto_migrate_module.auto_migrate("postgresql+psycopg://user:pass@db/app", force=True)

    assert calls == [
        ("auto_migrate_engine", "engine-for-postgresql+psycopg://user:pass@db/app"),
        ("auto_migrate_engine", "engine-for-postgresql+psycopg://user:pass@db/app"),
    ]

def test_postgres_psycopg_connect_args_include_transaction_safety_timeouts() -> None:
    args = session_module._engine_connect_args("postgresql+psycopg://user:pass@db/app")

    assert args["prepare_threshold"] is None
    options = str(args["options"])
    assert "idle_in_transaction_session_timeout=60000" in options
    assert "lock_timeout=10000" in options


def test_autocommit_sessionmaker_uses_autocommit_engine(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeEngine:
        def execution_options(self, **kwargs):
            seen["execution_options"] = kwargs
            return "autocommit-engine"

    def fake_sessionmaker(**kwargs):
        seen["sessionmaker"] = kwargs
        return "factory"

    monkeypatch.setattr(session_module, "get_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(session_module, "sessionmaker", fake_sessionmaker)

    result = session_module.get_autocommit_sessionmaker("postgresql+psycopg://user:pass@db/app")

    assert result == "factory"
    assert seen["execution_options"] == {"isolation_level": "AUTOCOMMIT"}
    assert seen["sessionmaker"] == {
        "bind": "autocommit-engine",
        "autocommit": False,
        "autoflush": False,
        "expire_on_commit": False,
    }
