from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from videoroll.utils.auto_youtube import parse_auto_youtube_created_by


AUTO_YOUTUBE_ORIGINS = frozenset({
    "auto_youtube",
    "youtube_home_scan",
    "youtube_task_restart",
})


@dataclass(frozen=True)
class TaskOriginContext:
    origin: str
    run_id: str | None = None
    restart_generation: int = 0

    @property
    def is_restart(self) -> bool:
        return self.origin == "youtube_task_restart"


def task_origin_context(task: Any) -> TaskOriginContext | None:
    """Read structured origin and fall back only for pre-migration rows."""
    origin = str(getattr(task, "origin_kind", None) or "").strip().lower()
    if origin in AUTO_YOUTUBE_ORIGINS:
        run_id = str(getattr(task, "origin_run_id", None) or "").strip() or None
        generation = max(0, int(getattr(task, "restart_generation", 0) or 0))
        return TaskOriginContext(origin=origin, run_id=run_id, restart_generation=generation)

    legacy = parse_auto_youtube_created_by(getattr(task, "created_by", None))
    if legacy is None:
        return None
    return TaskOriginContext(
        origin=str(legacy["origin"]),
        run_id=str(legacy.get("run_id") or "").strip() or None,
        restart_generation=1 if legacy["origin"] == "youtube_task_restart" else 0,
    )


def is_auto_youtube_task(task: Any) -> bool:
    return task_origin_context(task) is not None


def apply_task_origin(task: Any, *, origin: str, run_id: str | None) -> TaskOriginContext:
    normalized = str(origin or "").strip().lower()
    if normalized not in AUTO_YOUTUBE_ORIGINS:
        raise ValueError(f"unsupported task origin: {origin}")

    current = task_origin_context(task)
    generation = max(0, int(getattr(task, "restart_generation", 0) or 0))
    if normalized == "youtube_task_restart":
        generation = max(generation, current.restart_generation if current else 0) + 1

    clean_run_id = str(run_id or "").strip() or None
    task.origin_kind = normalized
    task.origin_run_id = clean_run_id
    task.restart_generation = generation
    return TaskOriginContext(
        origin=normalized,
        run_id=clean_run_id,
        restart_generation=generation,
    )
