from __future__ import annotations

import os
from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from videoroll.realtime import install_session_event_emitter


install_session_event_emitter()


def get_configured_database_url() -> str:
    # Database tooling (notably Alembic offline SQL generation) should not
    # require unrelated service settings such as Redis/S3 just to resolve the
    # database URL. Runtime services still fall back to the validated settings
    # object when DATABASE_URL is not explicitly present.
    direct = str(os.getenv("DATABASE_URL") or "").strip()
    if direct:
        return direct
    from videoroll.config import get_orchestrator_settings

    return get_orchestrator_settings().database_url


def _engine_connect_args(database_url: str) -> dict[str, object]:
    try:
        url = make_url(database_url)
    except Exception:
        return {}
    if url.get_backend_name() == "postgresql" and url.get_driver_name() == "psycopg":
        # PgBouncer/transaction-pooling setups can fail with
        # "prepared statement already exists" when psycopg auto-prepares queries.
        #
        # Keep a hard ceiling on accidental idle transactions and table-lock
        # waits. Long-running network/LLM work must not pin PostgreSQL locks.
        return {
            "prepare_threshold": None,
            "options": "-c idle_in_transaction_session_timeout=60000 -c lock_timeout=10000",
        }
    return {}


@lru_cache
def _get_engine_cached(database_url: str, pid: int) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, connect_args=_engine_connect_args(database_url))


def get_engine(database_url: str) -> Engine:
    # Celery prefork workers inherit module globals from the parent process.
    # Keep engines isolated per PID so child processes don't reuse the parent's pool.
    return _get_engine_cached(database_url, os.getpid())


def get_sessionmaker(database_url: str) -> sessionmaker[Session]:
    engine = get_engine(database_url)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_autocommit_sessionmaker(database_url: str) -> sessionmaker[Session]:
    """Session factory for long-lived agent work that spans external I/O.

    The ORM Session still provides the same API, but PostgreSQL statements are
    committed independently at the DBAPI layer. This prevents a dictionary or
    RAG SELECT/INSERT from leaving an idle transaction open while an agent waits
    on web/LLM requests for minutes.
    """
    engine = get_engine(database_url).execution_options(isolation_level="AUTOCOMMIT")
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def db_session(database_url: str) -> Generator[Session, None, None]:
    SessionLocal = get_sessionmaker(database_url)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
