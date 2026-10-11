#!/usr/bin/env python3
"""Non-destructive native deployment prerequisite check."""
from __future__ import annotations

import argparse
import os
from urllib.parse import urlsplit


def _database_identity(url: str) -> tuple[str, int, str]:
    # SQLAlchemy URLs may include a +driver suffix; compare the underlying DB.
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    if scheme not in {"postgresql", "postgres"}:
        raise ValueError("database connection must use PostgreSQL")
    host = (parts.hostname or "").lower()
    if host in {"localhost", "::1"}:
        host = "127.0.0.1"
    # A different role or an omitted default port does not create a new DB.
    return (host, parts.port or 5432, parts.path.strip("/"))


def check(*, connect: bool) -> None:
    required = (
        "DATABASE_URL",
        "HATCHET_DATABASE_URL",
        "INTERNAL_API_SECRET",
        "ADMIN_BOOTSTRAP_SECRET",
        "HATCHET_ADMIN_PASSWORD",
    )
    for key in required:
        value = os.environ.get(key, "").strip()
        if not value or "CHANGE_ME" in value:
            raise ValueError(f"{key} is required and must not contain the template placeholder")
    business = _database_identity(os.environ["DATABASE_URL"])
    hatchet = _database_identity(os.environ["HATCHET_DATABASE_URL"])
    if not business[2] or not hatchet[2] or business == hatchet:
        raise ValueError("Business and Hatchet must use different, named PostgreSQL databases")

    # Hatchet's login cookie is issued to browsers through the public gateway,
    # not its internal 8888 listener. Catch old Compose localhost settings.
    public_url = os.environ.get("HATCHET_SERVER_URL", "").strip()
    if public_url:
        public = urlsplit(public_url)
        if public.scheme not in {"http", "https"} or not public.hostname:
            raise ValueError("HATCHET_SERVER_URL must be a valid public http(s) origin")
        if public.hostname in {"localhost", "127.0.0.1"} and public.port == 3000:
            raise ValueError("HATCHET_SERVER_URL still points to the old Compose port 3000")
        cookie_domain = os.environ.get("HATCHET_COOKIE_DOMAIN", "").strip().lstrip(".").lower()
        hostname = public.hostname.lower()
        if not cookie_domain:
            raise ValueError("HATCHET_COOKIE_DOMAIN must match the public Hatchet URL host")
        if hostname != cookie_domain and not hostname.endswith("." + cookie_domain):
            raise ValueError("HATCHET_COOKIE_DOMAIN does not match HATCHET_SERVER_URL")
        frontend_url = os.environ.get("HATCHET_FRONTEND_URL", "").strip()
        if frontend_url:
            frontend = urlsplit(frontend_url)
            if frontend.scheme != public.scheme or frontend.hostname != public.hostname:
                raise ValueError("HATCHET_FRONTEND_URL must share the public Hatchet origin")
        if public.scheme == "https" and os.environ.get("HATCHET_COOKIE_INSECURE", "true").lower() in {"1", "true", "yes", "on"}:
            raise ValueError("HATCHET_COOKIE_INSECURE must be false for HTTPS")

    if connect:
        import psycopg

        # Check BOTH databases with a read-only query before any cutover.
        # psycopg DSNs use postgresql://, not SQLAlchemy's +psycopg suffix.
        for name in ("DATABASE_URL", "HATCHET_DATABASE_URL"):
            dsn = os.environ[name].replace("postgresql+psycopg://", "postgresql://", 1)
            with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    if cursor.fetchone() != (1,):
                        raise RuntimeError(f"{name} connectivity check failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", help="check both PostgreSQL databases with read-only SELECT")
    args = parser.parse_args()
    check(connect=args.connect)
    print("Native prerequisites passed (credential values not displayed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
