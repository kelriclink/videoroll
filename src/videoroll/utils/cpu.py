from __future__ import annotations

import math
import os
from pathlib import Path


def process_cpu_count() -> int:
    """
    Best-effort "CPU count available to this process".

    - Python 3.13+: prefer os.process_cpu_count()
    - Fallback: respect sched_getaffinity (cpuset)
    - Fallback: respect cgroup CPU quota (common in containers)
    - Fallback: os.cpu_count()
    """
    fn = getattr(os, "process_cpu_count", None)
    if callable(fn):
        try:
            n = int(fn() or 0)
            if n > 0:
                return n
        except Exception:
            pass

    candidates: list[int] = []

    try:
        affinity = os.sched_getaffinity(0)  # type: ignore[attr-defined]
        n = len(affinity)
        if n > 0:
            candidates.append(n)
    except Exception:
        pass

    quota_n = _cgroup_cpu_quota_count()
    if quota_n is not None and quota_n > 0:
        candidates.append(quota_n)

    try:
        n = int(os.cpu_count() or 0)
        if n > 0:
            candidates.append(n)
    except Exception:
        pass

    if not candidates:
        return 0
    return min(candidates)


def _cgroup_cpu_quota_count() -> int | None:
    """
    Estimate CPUs from cgroup quota.

    Returns None if no quota is detected.
    """
    # cgroup v2: /sys/fs/cgroup/cpu.max = "<quota> <period>" or "max <period>"
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    try:
        if cpu_max.exists():
            raw = cpu_max.read_text(encoding="utf-8").strip()
            quota_s, period_s = (raw.split() + ["", ""])[:2]
            if quota_s and period_s and quota_s != "max":
                quota = int(quota_s)
                period = int(period_s)
                if quota > 0 and period > 0:
                    return max(1, int(math.floor(quota / period)))
    except Exception:
        pass

    # cgroup v1: cpu.cfs_quota_us / cpu.cfs_period_us
    quota_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    try:
        if quota_path.exists() and period_path.exists():
            quota = int(quota_path.read_text(encoding="utf-8").strip())
            period = int(period_path.read_text(encoding="utf-8").strip())
            if quota > 0 and period > 0:
                return max(1, int(math.floor(quota / period)))
    except Exception:
        pass

    return None

