#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

from migrate_env import _quote_systemd


def set_value(path: Path, key: str, value: str) -> None:
    pattern = re.compile(rf"^{re.escape(key)}=")
    lines = path.read_text(encoding="utf-8").splitlines()
    replacement = f"{key}={_quote_systemd(value)}"
    output: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(replacement)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Set one key in a VideoRoll systemd EnvironmentFile")
    parser.add_argument("path", type=Path)
    parser.add_argument("key")
    parser.add_argument("value")
    args = parser.parse_args()
    set_value(args.path, args.key, args.value)
    print(f"Updated {args.key} in {args.path}; value was not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
