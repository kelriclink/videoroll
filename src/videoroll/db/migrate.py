from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

from alembic import command
from alembic.config import Config


logger = logging.getLogger(__name__)


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run VideoRoll database migrations")
    parser.add_argument("action", choices=("upgrade",))
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args(argv)

    try:
        with _alembic_config() as config:
            command.upgrade(config, args.revision)
    except Exception:
        logger.exception("database migration failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
