from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import JSON, Column, Index, MetaData, String, Table, UniqueConstraint, create_engine, inspect
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
LEGACY_IDS = {name: uuid.uuid5(uuid.NAMESPACE_URL, name) for name in ("task", "subtitle", "render", "publish")}


def _schema_snapshot(*, legacy: bool = False, sqlite: bool = True) -> MetaData:
    """Build a complete supported legacy schema, including referenced tasks.

    The former four-table fixture omitted required core columns and did not
    represent an application database. Only documented additive changes are
    omitted here; current model metadata is never modified.
    """
    missing_columns = {
        "tasks": {"lock_owner", "lock_until", "stopped_status", "active_publish_batch_id"},
        "app_settings": {"version"},
        "subtitle_jobs": {"lease_owner", "lease_until", "heartbeat_at", "operation_key"},
        "render_jobs": {"lease_owner", "lease_until", "heartbeat_at", "operation_key"},
        "publish_jobs": {
            "lease_owner", "lease_until", "heartbeat_at", "operation_key", "upload_progress", "upload_active",
            "batch_id", "platform", "account_id", "external_id", "external_url", "started_at", "finished_at",
        },
        "accounts": {"check_state", "last_checked_at", "last_check_message"},
        "youtube_sources": {
            "source_url", "display_name", "scan_interval_minutes", "scan_limit", "auto_process",
            "last_scan_started_at", "last_scan_finished_at", "last_scan_discovered_count", "last_scan_created_count",
            "last_scan_started_pipeline_count", "last_scan_skipped_duplicates", "last_scan_error",
            "scan_lock_owner", "scan_lock_until",
        },
    }
    snapshot = MetaData()
    for source in Base.metadata.sorted_tables:
        if legacy and source.name in SECURITY_TABLES | {"publish_batches"}:
            continue
        omitted = missing_columns.get(source.name, set()) if legacy else set()
        columns = [column._copy() for column in source.columns if column.name not in omitted]
        for column in columns:
            if sqlite and isinstance(column.type, postgresql.JSONB):
                column.type = JSON()
        table = Table(source.name, snapshot, *columns)
        for constraint in source.constraints:
            if isinstance(constraint, UniqueConstraint) and not omitted.intersection(constraint.columns.keys()):
                table.append_constraint(UniqueConstraint(*constraint.columns.keys(), name=constraint.name))
        for index in source.indexes:
            if not omitted.intersection(index.columns.keys()):
                Index(index.name, *(table.c[name] for name in index.columns.keys()), unique=index.unique)
    return snapshot


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
    legacy = _schema_snapshot(legacy=True)
    legacy.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            legacy.tables["tasks"].insert(),
            {"id": LEGACY_IDS["task"], "source_type": "local", "source_license": "own"},
        )
        connection.execute(
            legacy.tables["subtitle_jobs"].insert(),
            {"id": LEGACY_IDS["subtitle"], "task_id": LEGACY_IDS["task"], "status": "queued"},
        )
        connection.execute(
            legacy.tables["render_jobs"].insert(),
            {"id": LEGACY_IDS["render"], "task_id": LEGACY_IDS["task"], "status": "running"},
        )
        connection.execute(
            legacy.tables["publish_jobs"].insert(),
            {"id": LEGACY_IDS["publish"], "task_id": LEGACY_IDS["task"], "state": "draft"},
        )
        connection.execute(
            legacy.tables["app_settings"].insert(),
            {"key": "legacy.settings", "value_json": {"enabled": True}},
        )
    return database_url, engine


def _assert_legacy_rows_survive(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT status FROM subtitle_jobs WHERE id = ?", (LEGACY_IDS["subtitle"].hex,)
        ).scalar_one() == "queued"
        assert connection.exec_driver_sql(
            "SELECT status FROM render_jobs WHERE id = ?", (LEGACY_IDS["render"].hex,)
        ).scalar_one() == "running"
        assert connection.exec_driver_sql(
            "SELECT state FROM publish_jobs WHERE id = ?", (LEGACY_IDS["publish"].hex,)
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


def test_social_publisher_image_includes_packaging_migration_resources() -> None:
    dockerfile = (ROOT / "docker/social-publisher.Dockerfile").read_text(encoding="utf-8")

    assert "COPY alembic.ini ./alembic.ini" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert dockerfile.index("COPY migrations ./migrations") < dockerfile.index(
        "RUN pip install --no-cache-dir -e . --no-deps"
    )


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
    for table_name in Base.metadata.tables:
        assert f"CREATE TABLE {table_name}" in result.stdout
    assert "ALTER TABLE app_settings ADD COLUMN version" in result.stdout
    assert "ALTER TABLE subtitle_jobs ADD COLUMN lease_owner" in result.stdout
    assert "ALTER TABLE publish_jobs ADD COLUMN upload_progress" in result.stdout


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
    assert "upload_progress" in {column["name"] for column in inspector.get_columns("publish_jobs")}
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
    database_url = f"sqlite:///{tmp_path / 'missing-directory' / 'database.sqlite3'}"
    result = subprocess.run(
        [sys.executable, "-m", "videoroll.db.migrate", "upgrade"],
        cwd=ROOT,
        env=_migration_env(database_url, ROOT / "src"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0


def _run_upgrade(database_url: str, *, direct_alembic: bool = False, revision: str = "head") -> subprocess.CompletedProcess:
    command = (
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", revision]
        if direct_alembic
        else [sys.executable, "-m", "videoroll.db.migrate", "upgrade", revision]
    )
    return subprocess.run(
        command, cwd=ROOT, env=_migration_env(database_url, ROOT / "src"),
        capture_output=True, text=True, check=False, timeout=45,
    )


def _assert_complete_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    assert set(Base.metadata.tables).issubset(inspector.get_table_names())
    for table in Base.metadata.tables.values():
        assert set(table.columns.keys()).issubset({column["name"] for column in inspector.get_columns(table.name)})
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0005_operations_center"


def test_migration_initializes_empty_database_and_can_run_again(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.sqlite3'}"
    for direct in (False, True, False):
        result = _run_upgrade(database_url, direct_alembic=direct)
        assert result.returncode == 0, result.stderr
    _assert_complete_schema(create_engine(database_url))


def test_migration_adopts_previously_started_unversioned_database_without_losing_rows(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'previous-startup.sqlite3'}"
    engine = create_engine(database_url)
    snapshot = _schema_snapshot()
    snapshot.create_all(engine)
    with engine.begin() as connection:
        connection.execute(snapshot.tables["app_settings"].insert(), {"key": "keep", "value_json": {"a": 1}, "version": 7})
        connection.execute(snapshot.tables["operation_inbox"].insert(), {"id": uuid.uuid4(), "operation_key": "keep-op", "status": "done"})
    for direct in (False, True):
        result = _run_upgrade(database_url, direct_alembic=direct)
        assert result.returncode == 0, result.stderr
    _assert_complete_schema(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version FROM app_settings WHERE key = 'keep'").scalar_one() == 7
        assert connection.exec_driver_sql("SELECT status FROM operation_inbox WHERE operation_key = 'keep-op'").scalar_one() == "done"


def test_versioned_database_with_auto_migrated_columns_upgrades_again(tmp_path: Path) -> None:
    from videoroll.db.auto_migrate import _ensure_publish_jobs_generic_columns, _ensure_tasks_stop_columns

    database_url, engine = _create_legacy_database(tmp_path / "partially-versioned.sqlite3")
    first = _run_upgrade(database_url, revision="0001_security_architecture")
    assert first.returncode == 0, first.stderr
    _ensure_publish_jobs_generic_columns(engine)
    _ensure_tasks_stop_columns(engine)
    for direct in (True, False):
        result = _run_upgrade(database_url, direct_alembic=direct)
        assert result.returncode == 0, result.stderr
    _assert_complete_schema(engine)
    _assert_legacy_rows_survive(engine)


def test_migration_does_not_version_an_incompatible_unversioned_schema(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'incompatible.sqlite3'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE operation_inbox (id TEXT PRIMARY KEY, operation_key TEXT)")
        connection.exec_driver_sql("INSERT INTO operation_inbox VALUES ('keep-id', 'keep-op')")
    result = _run_upgrade(database_url)
    assert result.returncode != 0
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT operation_key FROM operation_inbox").scalar_one() == "keep-op"
        if inspect(connection).has_table("alembic_version"):
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").all() == []


def test_runtime_initializer_and_cli_use_the_same_versioned_schema(tmp_path: Path) -> None:
    from videoroll.db import migrate

    database_url = f"sqlite:///{tmp_path / 'runtime.sqlite3'}"
    assert callable(getattr(migrate, "initialize_database", None))
    migrate.initialize_database(database_url)
    result = _run_upgrade(database_url)
    assert result.returncode == 0, result.stderr
    migrate.initialize_database(database_url, force=True)
    _assert_complete_schema(create_engine(database_url))


def test_migration_rejects_missing_security_uniqueness_without_deleting_rows(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'missing-uniqueness.sqlite3'}"
    engine = create_engine(database_url)
    snapshot = _schema_snapshot()
    inbox = snapshot.tables["operation_inbox"]
    for constraint in list(inbox.constraints):
        if isinstance(constraint, UniqueConstraint):
            inbox.constraints.remove(constraint)
    snapshot.create_all(engine)
    with engine.begin() as connection:
        connection.execute(inbox.insert(), [{"operation_key": "duplicate"}, {"operation_key": "duplicate"}])
    result = _run_upgrade(database_url)
    assert result.returncode != 0
    assert "operation_inbox requires a UNIQUE constraint" in result.stderr
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM operation_inbox").scalar_one() == 2
        assert not inspect(connection).has_table("alembic_version")


def test_versioned_database_is_revalidated_instead_of_trusting_its_revision(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'invalid-versioned.sqlite3'}"
    engine = create_engine(database_url)
    snapshot = _schema_snapshot()
    snapshot.tables["publish_jobs"].c.upload_progress.type = String(32)
    snapshot.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0003_task_stop_controls')")
    result = _run_upgrade(database_url)
    assert result.returncode != 0
    assert "publish_jobs.upload_progress has type" in result.stderr
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0003_task_stop_controls"


def test_alembic_current_does_not_initialize_or_modify_an_empty_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'read-only-current.sqlite3'}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "current"],
        cwd=tmp_path, env=_migration_env(database_url, ROOT / "src"),
        capture_output=True, text=True, check=False, timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert inspect(create_engine(database_url)).get_table_names() == []
