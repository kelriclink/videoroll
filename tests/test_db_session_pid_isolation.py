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
