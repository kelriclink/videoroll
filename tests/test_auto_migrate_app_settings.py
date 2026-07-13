from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

from videoroll.db.auto_migrate import _ensure_app_settings_version_column


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
