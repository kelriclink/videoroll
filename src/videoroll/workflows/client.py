from __future__ import annotations

from typing import Any


class HatchetDependencyError(RuntimeError):
    pass


def create_hatchet_client() -> Any:
    """Create the production Hatchet client lazily.

    Hatchet intentionally lives in a dedicated worker image during migration so
    its protobuf/grpc dependency set cannot perturb the existing VideoRoll image.
    The SDK reads HATCHET_CLIENT_TOKEN and connection metadata from the process
    environment.
    """

    try:
        from hatchet_sdk import Hatchet
    except ImportError as exc:  # pragma: no cover - exercised in the dedicated image
        raise HatchetDependencyError(
            "hatchet-sdk is not installed in this runtime; use docker/workflow.Dockerfile"
        ) from exc
    return Hatchet()
