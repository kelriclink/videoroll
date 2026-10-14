"""Native dubbing service readiness. No GPU model imports in the API."""
from __future__ import annotations
import os
import subprocess

def worker_status() -> dict[str, str | bool]:
    unit = os.environ.get("VIDEOROLL_DUBBING_UNIT", "videoroll@dubbing-workflow-worker.service")
    try:
        probe = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True, timeout=2)
        available = probe.returncode == 0 and probe.stdout.strip() == "active"
        return {"available": available, "state": probe.stdout.strip() or "unknown"}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {"available": False, "state": "unavailable"}
