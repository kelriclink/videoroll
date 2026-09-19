from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.engine import Connection


def _validate_columns(connection: Connection, table_name: str, columns: Sequence[sa.Column]) -> None:
    existing = {column["name"]: column for column in sa.inspect(connection).get_columns(table_name)}
    for column in columns:
        actual = existing.get(column.name)
        if actual is None:
            raise RuntimeError(f"Incompatible existing schema: {table_name}.{column.name} is missing")
        expected_type = column.type.dialect_impl(connection.dialect)
        actual_type = actual["type"]
        compatible = expected_type._type_affinity is actual_type._type_affinity
        if connection.dialect.name == "sqlite" and isinstance(expected_type, sa.Uuid):
            compatible = isinstance(actual_type, sa.String) and actual_type.length in {32, 36}
        if not compatible:
            raise RuntimeError(f"Incompatible existing schema: {table_name}.{column.name} has type {actual_type}")
        if isinstance(expected_type, sa.String) and not isinstance(column.type, sa.Enum) and expected_type.length:
            if actual_type.length is None or actual_type.length < expected_type.length:
                raise RuntimeError(f"Incompatible existing schema: {table_name}.{column.name} has an incompatible length")
        if connection.dialect.name == "postgresql" and isinstance(expected_type, sa.DateTime):
            if expected_type.timezone != actual_type.timezone:
                raise RuntimeError(f"Incompatible existing schema: {table_name}.{column.name} has an incompatible timezone")
        if not column.nullable and actual["nullable"]:
            raise RuntimeError(f"Incompatible existing schema: {table_name}.{column.name} must be NOT NULL")


def _validate_table(connection: Connection, table: sa.Table) -> None:
    inspector = sa.inspect(connection)
    _validate_columns(connection, table.name, list(table.columns))
    primary_key = tuple(inspector.get_pk_constraint(table.name).get("constrained_columns") or ())
    if primary_key != tuple(table.primary_key.columns.keys()):
        raise RuntimeError(f"Incompatible existing schema: {table.name} has an incompatible primary key")
    unique_sets = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(table.name)
    } | {
        tuple(item["column_names"])
        for item in inspector.get_indexes(table.name)
        if item.get("unique") and item.get("dialect_options", {}).get("postgresql_where") is None
        and item.get("dialect_options", {}).get("sqlite_where") is None
    }
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint) and tuple(constraint.columns.keys()) not in unique_sets:
            names = ", ".join(constraint.columns.keys())
            raise RuntimeError(f"Incompatible existing schema: {table.name} requires a UNIQUE constraint on ({names})")


def create_table_if_missing(table_name: str, *elements: sa.SchemaItem) -> None:
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table(table_name):
        op.create_table(table_name, *elements)
        return
    # Existing create_all deployments must satisfy the revision, including its
    # uniqueness guarantees, before Alembic is allowed to record it as applied.
    _validate_table(op.get_bind(), sa.Table(table_name, sa.MetaData(), *elements))


def add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not context.is_offline_mode():
        existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}
        if column.name in existing:
            _validate_columns(op.get_bind(), table_name, [column])
            return
    op.add_column(table_name, column)


def create_index_if_missing(name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
    if not context.is_offline_mode():
        existing = {index["name"]: index for index in sa.inspect(op.get_bind()).get_indexes(table_name)}
        if name in existing:
            index = existing[name]
            if index["column_names"] != columns or bool(index["unique"]) != unique:
                raise RuntimeError(f"Incompatible existing schema: index {name} has a different definition")
            return
    op.create_index(name, table_name, columns, unique=unique)


def validate_application_schema(connection: Connection) -> None:
    from videoroll.db.base import Base
    from videoroll.db.legacy_schema import legacy_metadata

    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    legacy_tables = set(legacy_metadata().tables)
    revision_columns = {
        "app_settings": {"version"},
        "tasks": {"stopped_status", "queue_position"},
        "subtitle_jobs": {"lease_owner", "lease_until", "heartbeat_at", "operation_key"},
        "render_jobs": {"lease_owner", "lease_until", "heartbeat_at", "operation_key"},
        "publish_jobs": {"lease_owner", "lease_until", "heartbeat_at", "operation_key", "upload_progress", "upload_active"},
    }
    for table in Base.metadata.tables.values():
        if table.name not in tables:
            raise RuntimeError(f"Incompatible existing schema: table {table.name} is missing")
        missing = set(table.columns.keys()) - {column["name"] for column in inspector.get_columns(table.name)}
        if missing:
            raise RuntimeError(f"Incompatible existing schema: {table.name} is missing columns {', '.join(sorted(missing))}")
        if table.name not in legacy_tables:
            _validate_table(connection, table)
        elif table.name in revision_columns:
            _validate_columns(connection, table.name, [table.c[name] for name in sorted(revision_columns[table.name])])
