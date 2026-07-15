from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

from videoroll.db.auto_migrate import (
    _ensure_app_settings_version_column,
    _ensure_job_lease_columns,
    _ensure_publish_jobs_generic_columns,
)


def test_auto_migrate_adds_optimistic_version_to_legacy_app_settings() -> None:
    engine = create_engine("sqlite://")
    legacy = MetaData()
    Table(
        "app_settings",
        legacy,
        Column("key", String(128), primary_key=True),
        Column("value_json", String, nullable=False),
    )
    legacy.create_all(engine)

    _ensure_app_settings_version_column(engine)

    columns = {column["name"]: column for column in inspect(engine).get_columns("app_settings")}
    assert columns["version"]["nullable"] is False
    assert columns["version"]["default"] == "1"


def test_auto_migrate_adds_lease_columns_to_legacy_job_tables() -> None:
    engine = create_engine("sqlite://")
    legacy = MetaData()
    for table_name in ("subtitle_jobs", "render_jobs", "publish_jobs"):
        Table(
            table_name,
            legacy,
            Column("id", String(36), primary_key=True),
            Column("status", String(32), nullable=False),
            Column("created_at", String(64), nullable=False),
        )
    legacy.create_all(engine)

    _ensure_job_lease_columns(engine)

    for table_name in ("subtitle_jobs", "render_jobs", "publish_jobs"):
        columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
        assert {"lease_owner", "lease_until", "heartbeat_at", "operation_key"}.issubset(columns)


def test_auto_migrate_adds_bilibili_upload_progress_to_legacy_publish_jobs() -> None:
    engine = create_engine("sqlite://")
    legacy = MetaData()
    Table(
        "publish_jobs",
        legacy,
        Column("id", String(36), primary_key=True),
        Column("state", String(32), nullable=False),
        Column("created_at", String(64), nullable=False),
    )
    legacy.create_all(engine)

    _ensure_publish_jobs_generic_columns(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("publish_jobs")}
    assert {"upload_progress", "upload_active"}.issubset(columns)
