from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import QueuePool

from videoroll.db import migrate


@pytest.mark.parametrize(
    ("module_name", "settings_name", "startup_name"),
    [
        ("youtube_ingest.main", "get_youtube_ingest_settings", "_startup"),
        ("bilibili_publisher.main", "get_bilibili_publisher_settings", "_startup"),
        ("social_publisher.main", "get_social_publisher_settings", "_startup"),
        ("subtitle_service.main", "get_subtitle_settings", "_startup"),
        ("orchestrator_api.infrastructure.lifecycle", "get_orchestrator_settings", "initialize_runtime"),
    ],
)
def test_services_fail_startup_when_schema_initialization_fails(monkeypatch, module_name, settings_name, startup_name):
    module = importlib.import_module(f"videoroll.apps.{module_name}")
    settings = SimpleNamespace(database_url="sqlite://", trusted_proxy_cidrs="", trusted_proxy_hosts="")
    monkeypatch.setattr(module, settings_name, lambda: settings)
    monkeypatch.setattr(module, "service_token", lambda _settings: "test-service-token")
    if hasattr(module, "validate_bootstrap_secret"):
        monkeypatch.setattr(module, "validate_bootstrap_secret", lambda _settings: None)

    def fail_initialization(database_url):
        assert database_url == "sqlite://"
        raise RuntimeError("incompatible-test-schema")

    assert hasattr(module, "initialize_database")
    monkeypatch.setattr(module, "initialize_database", fail_initialization)
    with pytest.raises(RuntimeError, match="incompatible-test-schema"):
        if startup_name == "initialize_runtime":
            module.initialize_runtime(SimpleNamespace(state=SimpleNamespace()))
        else:
            module._startup()


def test_initializer_does_not_borrow_another_connection_during_upgrade(tmp_path):
    # A single-slot pool catches compatibility helpers that open another
    # connection while Alembic owns the migration transaction and advisory lock.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'single-connection.sqlite3'}",
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.1,
    )
    migrate.upgrade_database(engine)
    migrate.upgrade_database(engine)
    assert inspect(engine).has_table("alembic_version")


def test_initializer_cache_is_per_process_and_force_rechecks(monkeypatch):
    migrate._initialize_cached.cache_clear()

    calls = []
    engine = object()
    monkeypatch.setattr(migrate, "get_engine", lambda _url: engine)
    monkeypatch.setattr(migrate, "upgrade_database", lambda value: calls.append(value))
    monkeypatch.setattr(migrate.os, "getpid", lambda: 101)
    migrate.initialize_database("sqlite://test-cache")
    migrate.initialize_database("sqlite://test-cache")
    assert calls == [engine]
    monkeypatch.setattr(migrate.os, "getpid", lambda: 102)
    migrate.initialize_database("sqlite://test-cache")
    migrate.initialize_database("sqlite://test-cache", force=True)
    assert calls == [engine, engine, engine]
    migrate._initialize_cached.cache_clear()


def test_concurrent_initializers_do_not_share_alembic_global_context(monkeypatch, tmp_path):
    real_upgrade = migrate.command.upgrade
    active = 0
    peak_active = 0
    state_lock = threading.Lock()
    start = threading.Barrier(2)

    def measured_upgrade(config, revision):
        nonlocal active, peak_active
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
            overlapping = active > 1
        try:
            # Fail before corrupting Alembic's module-global context proxies;
            # both successful calls still perform a real database upgrade.
            assert not overlapping, "Concurrent calls entered Alembic's shared context"
            time.sleep(0.1)
            return real_upgrade(config, revision)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(migrate.command, "upgrade", measured_upgrade)

    def initialize(index):
        start.wait(timeout=5)
        engine = create_engine(f"sqlite:///{tmp_path / f'thread-{index}.sqlite3'}")
        try:
            migrate.upgrade_database(engine)
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0005_operations_center"
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(initialize, index) for index in range(2)]
        for result in results:
            result.result(timeout=15)
    assert peak_active == 1
