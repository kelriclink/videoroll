from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from importlib.resources import as_file, files
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from videoroll.db.session import get_configured_database_url, get_engine


logger = logging.getLogger(__name__)
_process_locks: dict[int, threading.Lock] = {}


@contextmanager
def _alembic_config() -> Iterator[Config]:
    root = Path(__file__).resolve().parents[3]
    source_config = root / "alembic.ini"
    source_migrations = root / "migrations"
    if source_config.is_file() and source_migrations.is_dir():
        yield Config(str(source_config))
        return

    packaged_root = files("videoroll").joinpath("_migration")
    with as_file(packaged_root) as migration_root:
        yield Config(str(migration_root / "alembic.ini"))


def upgrade_database(engine: Engine, revision: str = "head") -> None:
    """Use the same connection and Alembic environment as the deployment CLI."""
    # Alembic's context/op proxies are process globals, including before env.py
    # takes the DB lock. setdefault shares one lock across concurrent cache
    # misses; PID keys avoid using a parent's locked mutex after Celery forks.
    process_lock = _process_locks.setdefault(os.getpid(), threading.Lock())
    with process_lock, engine.connect() as connection, _alembic_config() as config:
        config.attributes["connection"] = connection
        config.attributes["configure_logger"] = False
        command.upgrade(config, revision)


@lru_cache
def _initialize_cached(database_url: str, pid: int) -> None:
    upgrade_database(get_engine(database_url))


def initialize_database(database_url: str, *, force: bool = False) -> None:
    """Initialize or upgrade a supported database once per worker process.

    Errors propagate so the service cannot start against an incomplete schema.
    Forced calls also validate already-versioned databases and repair supported
    legacy additive fields without skipping Alembic history.
    """
    if force:
        upgrade_database(get_engine(database_url))
    else:
        _initialize_cached(database_url, os.getpid())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run VideoRoll database migrations")
    parser.add_argument("action", choices=("upgrade",))
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args(argv)

    try:
        upgrade_database(get_engine(get_configured_database_url()), args.revision)
    except Exception:
        logger.exception("database migration failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
