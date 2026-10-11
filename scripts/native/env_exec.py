#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

from migrate_env import parse_env_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a command using a VideoRoll systemd EnvironmentFile")
    parser.add_argument("environment_file", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("a command is required")

    environment = os.environ.copy()
    environment.update(parse_env_file(args.environment_file))
    os.execvpe(args.command[0], args.command, environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
