from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from videoroll.utils.cpu import process_cpu_count


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def read_cpu_snapshot() -> tuple[int, int] | None:
    raw = _read_text(Path("/proc/stat"))
    if not raw:
        return None
    first = raw.splitlines()[0].split()
    if len(first) < 5 or first[0] != "cpu":
        return None
    try:
        values = [int(x) for x in first[1:]]
    except Exception:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent_between(prev: tuple[int, int], curr: tuple[int, int]) -> float | None:
    total_delta = curr[0] - prev[0]
    idle_delta = curr[1] - prev[1]
    if total_delta <= 0:
        return None
    busy = max(0, total_delta - max(0, idle_delta))
    return max(0.0, min(100.0, (busy / total_delta) * 100.0))


def sample_cpu_percent(*, interval_seconds: float = 0.05) -> float | None:
    prev = read_cpu_snapshot()
    if prev is None:
        return None
    time.sleep(max(0.0, float(interval_seconds)))
    curr = read_cpu_snapshot()
    if curr is None:
        return None
    return cpu_percent_between(prev, curr)


def read_load_average() -> list[float] | None:
    try:
        return [float(x) for x in os.getloadavg()]
    except Exception:
        return None


def _parse_meminfo(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except Exception:
            continue
        unit = parts[1].lower() if len(parts) > 1 else ""
        out[key] = value * 1024 if unit == "kb" else value
    return out


def memory_stats_from_meminfo(raw: str) -> dict[str, int | float] | None:
    data = _parse_meminfo(raw)
    total = int(data.get("MemTotal") or 0)
    if total <= 0:
        return None
    available = int(data.get("MemAvailable") or 0)
    if available <= 0:
        available = int(data.get("MemFree") or 0) + int(data.get("Buffers") or 0) + int(data.get("Cached") or 0)
    available = max(0, min(total, available))
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent": (used / total) * 100.0,
    }


def read_memory_stats() -> dict[str, int | float] | None:
    raw = _read_text(Path("/proc/meminfo"))
    if not raw:
        return None
    return memory_stats_from_meminfo(raw)


def read_cgroup_memory_stats() -> dict[str, int | float] | None:
    # cgroup v2
    current = _read_int(Path("/sys/fs/cgroup/memory.current"))
    max_raw = _read_text(Path("/sys/fs/cgroup/memory.max"))
    total = None if max_raw in {None, "", "max"} else _parse_positive_int(max_raw)
    if current is not None and total is not None and total > 0:
        return _memory_stats_from_usage(current, total)

    # cgroup v1
    current = _read_int(Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"))
    total = _read_int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    if current is not None and total is not None and 0 < total < 1 << 60:
        return _memory_stats_from_usage(current, total)
    return None


def _read_int(path: Path) -> int | None:
    return _parse_positive_int(_read_text(path))


def _parse_positive_int(raw: str | None) -> int | None:
    try:
        n = int(str(raw or "").strip())
    except Exception:
        return None
    return n if n >= 0 else None


def _memory_stats_from_usage(used: int, total: int) -> dict[str, int | float]:
    used = max(0, min(total, int(used)))
    available = max(0, total - used)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent": (used / total) * 100.0 if total > 0 else 0.0,
    }


def process_cpu_summary() -> dict[str, Any]:
    return {
        "percent": sample_cpu_percent(),
        "cores": process_cpu_count(),
        "load_average": read_load_average(),
    }
