"""Durable cache for a *completed* render whose upload/ack was interrupted.

Do not attempt to resume arbitrary partial MP4 files: without segment boundaries
and independently finalized media fragments, an interrupted FFmpeg output is
not a safe source of frames. Only fully returned encoder outputs are cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from videoroll.utils.hashing import sha256_file

logger = logging.getLogger(__name__)
_CACHE_VERSION = 1
_CACHE_TTL_SECONDS = 24 * 3600
_CACHE_MAX_BYTES = 40 * 1024**3


def cache_path(work_dir: Path, spec: Any, device: Any, suffix: str) -> Path:
    fingerprint = {
        "version": _CACHE_VERSION,
        "job_id": str(spec.render_job_id),
        "mode": spec.mode,
        "request": spec.request,
        "artifacts": sorted(
            ({"role": item.role, "key": item.storage_key, "sha256": item.sha256,
              "size": item.size_bytes} for item in spec.artifacts),
            key=lambda item: (item["role"], item["key"]),
        ),
        "device": {"id": device.id, "backend": device.backend, "index": device.index},
    }
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
    return work_dir / ".completed-renders" / str(spec.render_job_id) / f"{digest}{suffix}"


def _metadata_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def load_completed_render(path: Path) -> Path | None:
    """Use cached media only if the entire file matches its committed digest."""
    try:
        metadata = json.loads(_metadata_path(path).read_text(encoding="utf-8"))
        if (metadata.get("version") != _CACHE_VERSION or not path.is_file()
                or path.stat().st_size <= 0
                or path.stat().st_size != metadata.get("size_bytes")
                or sha256_file(path) != metadata.get("sha256")):
            return None
        return path
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def save_completed_render(path: Path, rendered: Path) -> Path:
    """Hardlink immutable finished media; never save partial encoder output."""
    size = rendered.stat().st_size
    if size <= 0 or size > _CACHE_MAX_BYTES:
        return rendered
    path.parent.mkdir(parents=True, exist_ok=True)
    if load_completed_render(path) is not None:
        return rendered
    # The per-execution directory and this cache live on the same worker FS.
    # Linking, instead of copying, avoids a second 10-GB write and is atomic.
    try:
        path.unlink(missing_ok=True)
        _metadata_path(path).unlink(missing_ok=True)
        os.link(rendered, path)
        metadata = {"version": _CACHE_VERSION, "size_bytes": size, "sha256": sha256_file(rendered)}
        temporary = _metadata_path(path).with_name(f"{_metadata_path(path).name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(metadata), encoding="utf-8")
            os.replace(temporary, _metadata_path(path))
        finally:
            temporary.unlink(missing_ok=True)
        return rendered
    except OSError:
        logger.warning("completed render checkpoint unavailable: %s", path, exc_info=True)
        return rendered


def prune_completed_renders(work_dir: Path, *, now: float | None = None) -> None:
    """Enforce a bounded temporary cache; an active upload remains usable via its open path."""
    root = work_dir / ".completed-renders"
    if not root.is_dir():
        return
    current = time.time() if now is None else now
    candidates: list[tuple[float, Path, int]] = []
    for path in root.glob("*/*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            if current - stat.st_mtime > _CACHE_TTL_SECONDS:
                path.unlink(missing_ok=True)
                continue
            if path.name.endswith(".json") or ".tmp" in path.name:
                continue
            candidates.append((stat.st_mtime, path, stat.st_size))
        except OSError:
            continue
    total = sum(size for _, _, size in candidates)
    for _, path, size in sorted(candidates):
        if total <= _CACHE_MAX_BYTES:
            break
        try:
            path.unlink(missing_ok=True)
            _metadata_path(path).unlink(missing_ok=True)
            total -= size
        except OSError:
            continue
