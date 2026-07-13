from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
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


def _migration_env(database_url: str, pythonpath: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_url,
            "PYTHONPATH": str(pythonpath),
            "REDIS_URL": "redis://localhost:6379/0",
            "S3_ENDPOINT_URL": "http://localhost:9000",
            "S3_ACCESS_KEY_ID": "test-access-key",
            "S3_SECRET_ACCESS_KEY": "test-secret-key",
            "S3_BUCKET": "test-bucket",
        }
    )
    return env


def _create_legacy_database(database_path: Path) -> tuple[str, Engine]:
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

    with engine.begin() as connection:
        connection.execute(
            legacy.tables["subtitle_jobs"].insert(),
            {"id": "subtitle-legacy", "status": "queued", "created_at": "2026-01-01T00:00:00Z"},
        )
        connection.execute(
            legacy.tables["render_jobs"].insert(),
            {"id": "render-legacy", "status": "running", "created_at": "2026-01-02T00:00:00Z"},
        )
        connection.execute(
            legacy.tables["publish_jobs"].insert(),
            {"id": "publish-legacy", "state": "draft", "created_at": "2026-01-03T00:00:00Z"},
        )
        connection.execute(
            legacy.tables["app_settings"].insert(),
            {"key": "legacy.settings", "value_json": {"enabled": True}},
        )
    return database_url, engine


def _assert_legacy_rows_survive(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT status FROM subtitle_jobs WHERE id = 'subtitle-legacy'"
        ).scalar_one() == "queued"
        assert connection.exec_driver_sql(
            "SELECT status FROM render_jobs WHERE id = 'render-legacy'"
        ).scalar_one() == "running"
        assert connection.exec_driver_sql(
            "SELECT state FROM publish_jobs WHERE id = 'publish-legacy'"
        ).scalar_one() == "draft"
        value_json, version = connection.exec_driver_sql(
            "SELECT value_json, version FROM app_settings WHERE key = 'legacy.settings'"
        ).one()
    assert json.loads(value_json) == {"enabled": True}
    assert version == 1


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


def test_docker_image_contains_migration_resources() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY alembic.ini ./alembic.ini" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile


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
    database_url, engine = _create_legacy_database(tmp_path / "source-security-schema.sqlite3")
    result = subprocess.run(
        [sys.executable, "-m", "videoroll.db.migrate", "upgrade"],
        cwd=ROOT,
        env=_migration_env(database_url, ROOT / "src"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    inspector = inspect(engine)
    assert SECURITY_TABLES.issubset(inspector.get_table_names())
    assert "version" in {column["name"] for column in inspector.get_columns("app_settings")}
    assert "lease_owner" in {column["name"] for column in inspector.get_columns("subtitle_jobs")}
    _assert_legacy_rows_survive(engine)


def test_installed_wheel_migration_cli_upgrades_legacy_database(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    build_env = os.environ.copy()
    build_env["PIP_CACHE_DIR"] = str(tmp_path / "pip-cache")
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(ROOT),
        ],
        cwd=tmp_path,
        env=build_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheel_path = next(wheel_dir.glob("videoroll-*.whl"))

    site_packages = tmp_path / "site-packages"
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(site_packages), str(wheel_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr

    database_url, engine = _create_legacy_database(tmp_path / "installed-security-schema.sqlite3")
    result = subprocess.run(
        [sys.executable, "-m", "videoroll.db.migrate", "upgrade"],
        cwd=tmp_path,
        env=_migration_env(database_url, site_packages),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert SECURITY_TABLES.issubset(inspect(engine).get_table_names())
    _assert_legacy_rows_survive(engine)


def test_migration_runner_returns_nonzero_on_failure(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.sqlite3'}"
    result = subprocess.run(
        [sys.executable, "-m", "videoroll.db.migrate", "upgrade"],
        cwd=ROOT,
        env=_migration_env(database_url, ROOT / "src"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
