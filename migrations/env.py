from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from videoroll.db import models as _models
from videoroll.db.auto_migrate import _ensure_postgres_enum_values, apply_legacy_migrations
from videoroll.db.base import Base
from videoroll.db.legacy_schema import create_legacy_tables
from videoroll.db.schema_compat import validate_application_schema
from videoroll.db.schema_lock import schema_lock
from videoroll.db.session import get_configured_database_url


config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_configured_database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _upgrade_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    if "destination_rev" not in context.get_context().opts:
        # Commands such as current/check/autogenerate only inspect the schema.
        # They must not bootstrap tables or run compatibility backfills.
        with context.begin_transaction():
            context.run_migrations()
        return
    with schema_lock(connection):
        # PostgreSQL enum labels must be committed before subsequent DML uses
        # them. The session lock continues to cover this preparatory transaction.
        _ensure_postgres_enum_values(connection)
        with connection.begin():
            if connection.dialect.name == "sqlite":
                # Python sqlite3's legacy transaction mode otherwise commits
                # DDL before the first DML statement, including a failed adoption.
                connection.exec_driver_sql("BEGIN")
            create_legacy_tables(connection)
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
            if set(context.get_context().get_current_heads()) == set(ScriptDirectory.from_config(config).get_heads()):
                apply_legacy_migrations(connection)
                validate_application_schema(connection)


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        _upgrade_connection(supplied_connection)
        return
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    try:
        with connectable.connect() as connection:
            _upgrade_connection(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
