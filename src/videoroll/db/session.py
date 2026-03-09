from __future__ import annotations

from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


def _engine_connect_args(database_url: str) -> dict[str, object]:
    try:
        url = make_url(database_url)
    except Exception:
        return {}
    if url.get_backend_name() == "postgresql" and url.get_driver_name() == "psycopg":
        # PgBouncer/transaction-pooling setups can fail with
        # "prepared statement already exists" when psycopg auto-prepares queries.
        return {"prepare_threshold": None}
    return {}


@lru_cache
def get_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, connect_args=_engine_connect_args(database_url))


def get_sessionmaker(database_url: str) -> sessionmaker[Session]:
    engine = get_engine(database_url)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def db_session(database_url: str) -> Generator[Session, None, None]:
    SessionLocal = get_sessionmaker(database_url)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
