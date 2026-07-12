from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from videoroll.db import models as db_models
from videoroll.db.base import Base


ROOT = Path(__file__).resolve().parents[1]
SECURITY_TABLES = {
    "outbox_events",
    "operation_inbox",
    "remote_api_requests",
    "desktop_access_grants",
    "security_audit_events",
}


def _unique_column_sets(table_name: str) -> set[tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_security_tables_and_unique_operation_keys_exist() -> None:
    metadata = Base.metadata

    assert SECURITY_TABLES.issubset(metadata.tables)
    assert ("operation_key",) in _unique_column_sets("operation_inbox")
    assert ("token_hash", "idempotency_key") in _unique_column_sets("remote_api_requests")


def test_security_models_use_uuid_timestamps_jsonb_and_bounded_errors() -> None:
    for table_name in SECURITY_TABLES:
        table = Base.metadata.tables[table_name]
        postgres_sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))

        assert " UUID " in postgres_sql
        assert "WITH TIME ZONE" in postgres_sql
        payload_columns = [column for column in table.columns if column.name.endswith("_json")]
        assert payload_columns
        assert all(isinstance(column.type, JSON) for column in payload_columns)
        assert "JSONB" in postgres_sql

        error_columns = [column for column in table.columns if "error" in column.name]
        assert all(isinstance(column.type, String) and column.type.length for column in error_columns)


def test_job_leases_and_optimistic_settings_version_exist() -> None:
    for table_name in ("subtitle_jobs", "render_jobs", "publish_jobs"):
        table = Base.metadata.tables[table_name]
        assert {"lease_owner", "lease_until", "heartbeat_at", "operation_key"}.issubset(table.columns.keys())
        assert any("lease" in (index.name or "") for index in table.indexes)

    app_settings = Base.metadata.tables["app_settings"]
    assert "version" in app_settings.columns
    assert app_settings.c.version.nullable is False


def test_migration_does_not_depend_on_auto_migrate() -> None:
    source = (ROOT / "src/videoroll/db/auto_migrate.py").read_text(encoding="utf-8")

    for table_name in SECURITY_TABLES:
        assert table_name not in source


def test_alembic_offline_sql_contains_security_schema() -> None:
    assert (ROOT / "alembic.ini").is_file()
    assert (ROOT / "migrations/env.py").is_file()
    assert (ROOT / "migrations/versions/0001_security_architecture.py").is_file()

    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql+psycopg://user:pass@db/videoroll"
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head", "--sql"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for table_name in SECURITY_TABLES:
        assert f"CREATE TABLE {table_name}" in result.stdout
    assert "ALTER TABLE app_settings ADD COLUMN version" in result.stdout
    assert "ALTER TABLE subtitle_jobs ADD COLUMN lease_owner" in result.stdout


def test_sqlite_migration_smoke(tmp_path: Path) -> None:
    assert (ROOT / "alembic.ini").is_file()
    database_path = tmp_path / "security-schema.sqlite3"
    database_url = f"sqlite:///{database_path}"
    engine = create_engine(database_url)
    legacy = MetaData()
    for table_name in ("subtitle_jobs", "render_jobs"):
        Table(
            table_name,
            legacy,
            Column("id", String(36), primary_key=True),
            Column("status", String(32), nullable=False),
            Column("created_at", String(64), nullable=False),
        )
    Table(
        "publish_jobs",
        legacy,
        Column("id", String(36), primary_key=True),
        Column("state", String(32), nullable=False),
        Column("created_at", String(64), nullable=False),
    )
    Table(
        "app_settings",
        legacy,
        Column("key", String(128), primary_key=True),
        Column("value_json", JSON, nullable=False),
    )
    legacy.create_all(engine)

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    inspector = inspect(engine)
    assert SECURITY_TABLES.issubset(inspector.get_table_names())
    assert "version" in {column["name"] for column in inspector.get_columns("app_settings")}
    assert "lease_owner" in {column["name"] for column in inspector.get_columns("subtitle_jobs")}


def test_migration_runner_returns_nonzero_on_failure(monkeypatch) -> None:
    assert (ROOT / "src/videoroll/db/migrate.py").is_file()
    migrate = importlib.import_module("videoroll.db.migrate")

    monkeypatch.setattr(migrate.command, "upgrade", lambda *_args, **_kwargs: None)
    assert migrate.main(["upgrade"]) == 0

    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrate.command, "upgrade", fail)
    assert migrate.main(["upgrade"]) != 0
