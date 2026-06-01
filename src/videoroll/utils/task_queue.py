from __future__ import annotations

from typing import Sequence, TypeVar


T = TypeVar("T")


def available_task_queue_capacity(max_concurrency: int, locked_task_count: int) -> int:
    try:
        max_concurrency = int(max_concurrency)
    except Exception:
        max_concurrency = 0
    try:
        locked_task_count = int(locked_task_count)
    except Exception:
        locked_task_count = 0
    return max(0, max_concurrency - max(0, locked_task_count))


def task_queue_slot_reserved_for(task_id: T, locked_task_ids: Sequence[T], max_concurrency: int) -> bool:
    limit = available_task_queue_capacity(max_concurrency, 0)
    if limit <= 0:
        return False
    return task_id in list(locked_task_ids[:limit])
