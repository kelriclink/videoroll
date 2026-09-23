from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowRuntimeSettings:
    worker_name: str = "videoroll-workflow-core-01"
    worker_slots: int = 4
    orchestrator_url: str = "http://orchestrator:8000"
    internal_api_secret: str = "videoroll-development-internal-secret"
    development_mode: bool = False

    @classmethod
    def from_env(cls) -> "WorkflowRuntimeSettings":
        worker_name = str(os.getenv("HATCHET_WORKER_NAME") or "videoroll-workflow-core-01").strip()
        if not worker_name:
            worker_name = "videoroll-workflow-core-01"
        try:
            worker_slots = int(os.getenv("HATCHET_WORKER_SLOTS") or "4")
        except (TypeError, ValueError) as exc:
            raise ValueError("HATCHET_WORKER_SLOTS must be an integer") from exc
        if worker_slots < 1 or worker_slots > 256:
            raise ValueError("HATCHET_WORKER_SLOTS must be in the range 1..256")

        orchestrator_url = str(os.getenv("ORCHESTRATOR_URL") or "http://orchestrator:8000").strip().rstrip("/")
        if not orchestrator_url:
            raise ValueError("ORCHESTRATOR_URL is required")
        internal_api_secret = str(os.getenv("INTERNAL_API_SECRET") or "videoroll-development-internal-secret").strip()
        development_mode = str(os.getenv("DEVELOPMENT_MODE") or "false").strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            worker_name=worker_name,
            worker_slots=worker_slots,
            orchestrator_url=orchestrator_url,
            internal_api_secret=internal_api_secret,
            development_mode=development_mode,
        )
