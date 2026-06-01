from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Mapping

WorkdirKind = Literal["subtitle", "render", "youtube"]

_ACTIVE_JOB_STATUSES = {"queued", "running"}


@dataclass(frozen=True)
class WorkdirJobState:
    task_id: uuid.UUID
    status: str


@dataclass(frozen=True)
class WorkdirScanEntry:
    kind: WorkdirKind
    owner_id: str
    rel_path: str
    size_bytes: int
    modified_at: datetime
    reclaimable: bool
    reason: str
    task_id: str | None = None


@dataclass(frozen=True)
class WorkdirScanResult:
    work_dir: str
    scanned_dirs: int
    reclaimable_dirs: int
    total_bytes: int
    reclaimable_bytes: int
    entries: list[WorkdirScanEntry]


@dataclass(frozen=True)
class WorkdirCleanupResult:
    deleted_dirs: int
    deleted_bytes: int
    deleted_paths: list[str]
    errors: list[str]


def _scan_tree(root: Path) -> tuple[int, float]:
    total_bytes = 0
    latest_mtime = 0.0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            stat = current.stat()
            latest_mtime = max(latest_mtime, float(stat.st_mtime or 0.0))
        except FileNotFoundError:
            continue
        except OSError:
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            latest_mtime = max(latest_mtime, float(entry.stat(follow_symlinks=False).st_mtime or 0.0))
                            continue
                        if entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            total_bytes += int(stat.st_size or 0)
                            latest_mtime = max(latest_mtime, float(stat.st_mtime or 0.0))
                    except FileNotFoundError:
                        continue
                    except OSError:
                        continue
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            try:
                total_bytes += int(stat.st_size or 0)
            except Exception:
                pass
        except OSError:
            continue
    return total_bytes, latest_mtime


def _parse_uuid(value: str) -> uuid.UUID | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except Exception:
        return None


def _is_recent(modified_at: datetime, *, now: datetime, recent_grace: timedelta) -> bool:
    return modified_at >= (now - recent_grace)


def scan_workdir(
    work_dir: Path,
    *,
    subtitle_jobs: Mapping[uuid.UUID, WorkdirJobState],
    render_jobs: Mapping[uuid.UUID, WorkdirJobState],
    known_task_ids: set[uuid.UUID],
    active_task_ids: set[uuid.UUID],
    now: datetime | None = None,
    recent_grace_seconds: int = 900,
) -> WorkdirScanResult:
    now_dt = now or datetime.now(tz=timezone.utc)
    recent_grace = timedelta(seconds=max(0, int(recent_grace_seconds or 0)))

    entries: list[WorkdirScanEntry] = []
    total_bytes = 0
    reclaimable_bytes = 0

    for kind in ("subtitle", "render", "youtube"):
        kind_dir = work_dir / kind
        if not kind_dir.is_dir():
            continue

        for child in sorted(kind_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue

            size_bytes, latest_mtime = _scan_tree(child)
            try:
                fallback_mtime = float(child.stat().st_mtime or 0.0)
            except Exception:
                fallback_mtime = 0.0
            modified_at = datetime.fromtimestamp(latest_mtime or fallback_mtime or now_dt.timestamp(), tz=timezone.utc)
            is_recent = _is_recent(modified_at, now=now_dt, recent_grace=recent_grace)
            owner_id = child.name
            rel_path = child.relative_to(work_dir).as_posix()
            reclaimable = False
            reason = ""
            task_id: str | None = None
            owner_uuid = _parse_uuid(owner_id)

            if kind == "subtitle":
                if owner_uuid is None:
                    reclaimable = not is_recent
                    reason = "invalid subtitle job directory name" if reclaimable else "recent invalid subtitle directory"
                else:
                    state = subtitle_jobs.get(owner_uuid)
                    if state is None:
                        reclaimable = not is_recent
                        reason = "subtitle job missing" if reclaimable else "recent unknown subtitle job directory"
                    else:
                        task_id = str(state.task_id)
                        if str(state.status or "").strip().lower() in _ACTIVE_JOB_STATUSES:
                            reason = f"subtitle job active ({state.status})"
                        else:
                            reclaimable = True
                            reason = f"subtitle job {state.status}"
            elif kind == "render":
                if owner_uuid is None:
                    reclaimable = not is_recent
                    reason = "invalid render job directory name" if reclaimable else "recent invalid render directory"
                else:
                    state = render_jobs.get(owner_uuid)
                    if state is None:
                        reclaimable = not is_recent
                        reason = "render job missing" if reclaimable else "recent unknown render job directory"
                    else:
                        task_id = str(state.task_id)
                        if str(state.status or "").strip().lower() in _ACTIVE_JOB_STATUSES:
                            reason = f"render job active ({state.status})"
                        else:
                            reclaimable = True
                            reason = f"render job {state.status}"
            else:
                if owner_uuid is None:
                    reclaimable = not is_recent
                    reason = "invalid youtube task directory name" if reclaimable else "recent invalid youtube temp directory"
                else:
                    task_id = str(owner_uuid)
                    if owner_uuid in active_task_ids:
                        reason = "task active"
                    elif owner_uuid not in known_task_ids:
                        reclaimable = not is_recent
                        reason = "youtube task missing" if reclaimable else "recent unknown youtube temp directory"
                    elif is_recent:
                        reason = "recent youtube temp directory"
                    else:
                        reclaimable = True
                        reason = "youtube temp directory idle"

            total_bytes += size_bytes
            if reclaimable:
                reclaimable_bytes += size_bytes
            entries.append(
                WorkdirScanEntry(
                    kind=kind,
                    owner_id=owner_id,
                    rel_path=rel_path,
                    size_bytes=size_bytes,
                    modified_at=modified_at,
                    reclaimable=reclaimable,
                    reason=reason,
                    task_id=task_id,
                )
            )

    entries.sort(key=lambda item: (not item.reclaimable, -item.size_bytes, item.rel_path))
    reclaimable_dirs = sum(1 for item in entries if item.reclaimable)
    return WorkdirScanResult(
        work_dir=str(work_dir),
        scanned_dirs=len(entries),
        reclaimable_dirs=reclaimable_dirs,
        total_bytes=total_bytes,
        reclaimable_bytes=reclaimable_bytes,
        entries=entries,
    )


def cleanup_reclaimable_dirs(work_dir: Path, entries: list[WorkdirScanEntry]) -> WorkdirCleanupResult:
    deleted_dirs = 0
    deleted_bytes = 0
    deleted_paths: list[str] = []
    errors: list[str] = []

    for entry in entries:
        if not entry.reclaimable:
            continue
        target = work_dir / entry.rel_path
        try:
            shutil.rmtree(target)
            deleted_dirs += 1
            deleted_bytes += int(entry.size_bytes or 0)
            deleted_paths.append(entry.rel_path)
        except FileNotFoundError:
            continue
        except Exception as exc:
            errors.append(f"{entry.rel_path}: {type(exc).__name__}: {exc}")

    return WorkdirCleanupResult(
        deleted_dirs=deleted_dirs,
        deleted_bytes=deleted_bytes,
        deleted_paths=deleted_paths,
        errors=errors,
    )
