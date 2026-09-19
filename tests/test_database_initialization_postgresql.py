"""Optional integration checks against an explicitly supplied disposable PG DB.

Set VIDEOROLL_MIGRATION_TEST_DATABASE_URL to a dedicated database whose name
starts with videoroll_migration_test. Tests create and delete their own databases
on that server; ordinary offline test runs skip this module.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url

from videoroll.db.base import Base
from videoroll.db.migrate import upgrade_database
from test_security_schema_migration import _migration_env, _schema_snapshot


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database_url():
    configured = os.environ.get("VIDEOROLL_MIGRATION_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("A disposable PostgreSQL server was not supplied")
    admin_url = make_url(configured)
    if not (admin_url.database or "").startswith("videoroll_migration_test"):
        pytest.fail("Integration tests require an explicitly named disposable migration-test database")
    database_name = "videoroll_migration_test_" + uuid.uuid4().hex[:12]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        yield admin_url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        admin.dispose()


def _concurrent_startup(database_url: str, cwd: Path) -> None:
    env = _migration_env(database_url, ROOT / "src")
    runtime = [
        sys.executable, "-c",
        "import os; from videoroll.db.migrate import initialize_database; "
        "initialize_database(os.environ['DATABASE_URL'], force=True)",
    ]
    commands = [runtime, [sys.executable, "-m", "videoroll.db.migrate", "upgrade"], runtime]
    processes = [subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for command in commands]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=45)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_concurrent_postgres_runtime_and_cli_initialization(database_url, tmp_path):
    _concurrent_startup(database_url, tmp_path)
    engine = create_engine(database_url)
    try:
        assert set(Base.metadata.tables).issubset(inspect(engine).get_table_names())
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO app_settings (key, value_json, version) VALUES ('keep', '{}', 9)"))
        _concurrent_startup(database_url, tmp_path)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0005_operations_center"
            assert connection.execute(text("SELECT version FROM app_settings WHERE key = 'keep'")).scalar_one() == 9
    finally:
        engine.dispose()


def test_postgres_offline_sql_bootstraps_an_empty_database(database_url, tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head", "--sql"],
        cwd=tmp_path, env=_migration_env(database_url, ROOT / "src"),
        capture_output=True, text=True, check=False, timeout=45,
    )
    assert result.returncode == 0, result.stderr
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(result.stdout)
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0005_operations_center"
        assert set(Base.metadata.tables).issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.mark.parametrize("legacy", [True, False])
def test_postgres_adopts_legacy_or_unversioned_schema_under_one_connection_lock(database_url, legacy):
    engine = create_engine(database_url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    snapshot = _schema_snapshot(legacy=legacy, sqlite=False)
    snapshot.create_all(engine)
    with engine.begin() as connection:
        connection.execute(snapshot.tables["app_settings"].insert(), {"key": "keep", "value_json": {"preserved": True}})
    ddl_connections = set()

    def check_lock(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "UPDATE ", "INSERT ")):
            pid, held = connection.execute(
                text("SELECT pg_backend_pid(), EXISTS (SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory')")
            ).one()
            assert held, f"Schema work ran without its PostgreSQL advisory lock: {statement}"
            ddl_connections.add(pid)

    event.listen(engine, "before_cursor_execute", check_lock)
    try:
        upgrade_database(engine)
        upgrade_database(engine)
        assert len(ddl_connections) == 1
        with engine.connect() as connection:
            assert connection.execute(text("SELECT value_json FROM app_settings WHERE key = 'keep'")).scalar_one() == {"preserved": True}
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0005_operations_center"
            assert not connection.execute(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory')")).scalar_one()
    finally:
        event.remove(engine, "before_cursor_execute", check_lock)
        engine.dispose()
