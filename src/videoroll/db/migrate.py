from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from alembic import command
from alembic.config import Config


logger = logging.getLogger(__name__)


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[3]
    return Config(str(root / "alembic.ini"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run VideoRoll database migrations")
    parser.add_argument("action", choices=("upgrade",))
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args(argv)

    try:
        command.upgrade(_alembic_config(), args.revision)
    except Exception:
        logger.exception("database migration failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
