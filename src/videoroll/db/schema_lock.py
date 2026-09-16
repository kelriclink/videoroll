from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection


# Retain the legacy auto-migration key so mixed-version service startups also
# serialize their schema changes while a deployment is being upgraded.
SCHEMA_ADVISORY_LOCK_KEY = 0x564944454F524F4C


@contextmanager
def schema_lock(connection: Connection) -> Iterator[None]:
    """Hold the PostgreSQL session lock on the connection doing the migration."""
    if connection.dialect.name != "postgresql":
        yield
        return
    if connection.in_transaction():
        raise RuntimeError("Schema initialization requires a connection without an active transaction")
    try:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_ADVISORY_LOCK_KEY})
        connection.commit()
    except Exception:
        # A failed commit/connection can leave acquisition's outcome uncertain.
        connection.invalidate()
        raise
    try:
        yield
    finally:
        # A failed DDL statement leaves PostgreSQL's transaction aborted. Roll
        # that back before unlocking; session locks survive transaction rollback.
        connection.rollback()
        try:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEMA_ADVISORY_LOCK_KEY})
            connection.commit()
        except Exception:
            # Never return a session holding our lock to the connection pool.
            connection.invalidate()
            raise
