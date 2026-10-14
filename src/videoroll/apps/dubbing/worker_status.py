"""Unified service and Hatchet workflow readiness, with bounded local probes."""
from __future__ import annotations
import os
import subprocess
import httpx


def worker_status() -> dict[str, str | bool]:
    unit = os.environ.get('VIDEOROLL_DUBBING_UNIT', 'videoroll@dubbing-workflow-worker.service')
    try:
        probe = subprocess.run(['systemctl', 'is-active', unit],
                               capture_output=True, text=True, timeout=2)
        worker_ready = probe.returncode == 0 and probe.stdout.strip() == 'active'
        from videoroll.apps.dubbing.service_client import base_url
        result = httpx.get(base_url() + '/health', timeout=1.5)
        service_ready = result.status_code == 200 and result.json().get('ok') is True
        return {'available': worker_ready and service_ready,
                'state': probe.stdout.strip() or 'unknown',
                'service_ready': service_ready, 'service_state': 'active' if service_ready else 'unavailable'}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError, httpx.HTTPError):
        return {'available': False, 'state': 'unavailable',
                'service_ready': False, 'service_state': 'unavailable'}
