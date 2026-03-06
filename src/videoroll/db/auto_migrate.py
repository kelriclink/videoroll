from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from videoroll.db.session import get_engine


logger = logging.getLogger(__name__)


def _is_duplicate_column_error(exc: Exception) -> bool:
    """
    Best-effort detection across dialects/drivers.

    We want auto-migrations to be safe under concurrent startups where two
    processes may race to add the same column.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate == "42701":  # PostgreSQL duplicate_column
        return True

    msg = str(exc).lower()
    return ("duplicate column" in msg) or ("already exists" in msg and "column" in msg)


def _add_column(engine: Engine, table: str, column: str, column_type_sql: str) -> None:
    stmt = f"ALTER TABLE {table} ADD COLUMN {column} {column_type_sql}"
    with engine.begin() as conn:
        try:
            conn.execute(text(stmt))
        except Exception as e:
            if _is_duplicate_column_error(e):
                return
            raise


def _ensure_tasks_lock_columns(engine: Engine) -> None:
    insp = inspect(engine)
    if "tasks" not in set(insp.get_table_names()):
        return

    cols = {c.get("name") for c in insp.get_columns("tasks")}
    dialect = (engine.dialect.name or "").lower()

    if "lock_owner" not in cols:
        _add_column(engine, "tasks", "lock_owner", "VARCHAR(128)")
        logger.warning("auto-migrated DB: added tasks.lock_owner")

    if "lock_until" not in cols:
        ts_type = "TIMESTAMPTZ" if dialect == "postgresql" else "TIMESTAMP"
        _add_column(engine, "tasks", "lock_until", ts_type)
        logger.warning("auto-migrated DB: added tasks.lock_until")


def auto_migrate_engine(engine: Engine) -> None:
    """
    Small, targeted "auto migration" for deployments without Alembic.

    Today it only ensures columns required by the task-level queue exist.
    """
    _ensure_tasks_lock_columns(engine)


@lru_cache
def auto_migrate(database_url: str) -> None:
    """
    Idempotent auto migration (cached per database_url).
    """
    engine = get_engine(database_url)
    auto_migrate_engine(engine)

