"""Opt-in genuine runtime test: never replace services with mocked responses.

Run on an isolated Linux VM with real PostgreSQL, Redis, nginx, VideoRoll
and Hatchet already installed:
    VIDEOROLL_NATIVE_REAL_SMOKE=1 pytest -q tests/test_native_live_smoke.py
The test is skipped in ordinary fast unit-test jobs.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    os.environ.get("VIDEOROLL_NATIVE_REAL_SMOKE") != "1",
    reason="requires running native Linux services; set VIDEOROLL_NATIVE_REAL_SMOKE=1",
)
def test_real_postgres_redis_and_core_through_native_nginx() -> None:
    command = ["bash", str(ROOT / "scripts/native/smoke_runtime.sh"), "core"]
    if os.geteuid() != 0:
        command.insert(0, "sudo")
        command.insert(1, "-n")
    result = subprocess.run(command, capture_output=True, text=True, timeout=80)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "Native core smoke PASSED" in result.stdout
