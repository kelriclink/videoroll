"""Small, dependency-free deployment environment guardrails.

Compose performs the first validation through required variable expansion.  This
module is the second line of defence for images started directly or with a
different Compose implementation.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


_KNOWN_DEFAULTS = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "videoroll",
        "videorollsecret",
        "videoroll-development-internal-secret",
        "videoroll-development-bootstrap-secret",
    }
)
_S3_KEYS = ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
_INTERNAL_KEYS = ("INTERNAL_API_SECRET", "ADMIN_BOOTSTRAP_SECRET")


def _is_non_default(value: object) -> bool:
    return str(value or "").strip().lower() not in _KNOWN_DEFAULTS


def validate_deployment_secrets(values: Mapping[str, object], *, production: bool) -> bool:
    """Return whether a full application deployment has safe credentials."""
    if not production:
        return True
    return all(_is_non_default(values.get(name)) for name in (*_S3_KEYS, *_INTERNAL_KEYS))


def validate_runtime_environment(values: Mapping[str, object], *, role: str, production: bool) -> bool:
    """Validate only credentials the selected process role is expected to have."""
    if not production:
        return True
    required = list(_INTERNAL_KEYS)
    if role != "egress-gateway":
        required.extend(_S3_KEYS)
    return all(_is_non_default(values.get(name)) for name in required)


def _development_mode(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    role = str(os.getenv("DEPLOYMENT_ROLE") or "application").strip() or "application"
    production = not _development_mode(os.getenv("DEVELOPMENT_MODE"))
    if validate_runtime_environment(os.environ, role=role, production=production):
        return 0
    print("deployment refused: required credentials are empty or use a known default", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
