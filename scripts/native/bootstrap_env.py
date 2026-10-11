#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
import tempfile
from pathlib import Path

from migrate_env import _quote_systemd, native_data_paths, parse_env_file


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy" / "systemd" / "videoroll.env.example"


def _hatchet_admin_password() -> str:
    # Hatchet requires 8..64 chars with upper, lower and decimal digit.
    # Include all three classes deterministically rather than relying on the
    # statistical distribution of token_urlsafe.
    return "Aa1" + secrets.token_urlsafe(32)


def build_environment(database_url: str, hatchet_database_url: str) -> dict[str, str]:
    values = parse_env_file(TEMPLATE)
    values.update(native_data_paths(ROOT))
    values["DATABASE_URL"] = database_url.strip()
    values["HATCHET_DATABASE_URL"] = hatchet_database_url.strip()
    values["INTERNAL_API_SECRET"] = secrets.token_urlsafe(48)
    values["ADMIN_BOOTSTRAP_SECRET"] = secrets.token_urlsafe(48)
    values["HATCHET_ADMIN_PASSWORD"] = _hatchet_admin_password()
    values["HATCHET_CLIENT_TOKEN"] = ""
    return values


def write_environment(values: dict[str, str], output: Path, *, force: bool) -> None:
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# Generated for a database-only VideoRoll restore.\n"
        "# PostgreSQL is the only durable state required from the previous deployment.\n"
        + "".join(f"{key}={_quote_systemd(value)}\n" for key, value in values.items())
    )
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a fresh native VideoRoll environment around an existing business DB")
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--hatchet-database-url",
        required=True,
        help="URL of an empty/rebuildable PostgreSQL database used only by Hatchet",
    )
    parser.add_argument("--output", type=Path, default=Path("/opt/videoroll/config/videoroll.env"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    write_environment(
        build_environment(args.database_url, args.hatchet_database_url),
        args.output,
        force=args.force,
    )
    print(f"Fresh native environment written to {args.output}; generated secret values were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
