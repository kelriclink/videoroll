"""Persistent VideoRoll control plane for upstream YouDub VoxCPM2.

The engine is launched on demand inside the existing dubbing workflow worker,
so an enabled engine does NOT permanently occupy VRAM. Disabling blocks new
jobs and never kills a live YouDub task.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from sqlalchemy.orm import Session

from videoroll.apps.dubbing.youdub_native import model_installed, runtime_dir
from videoroll.db.models import DubbingJob

BUSY_STATES = (
    "queued", "preparing", "synthesizing", "aligning", "mixing",
    "pause_requested", "cancel_requested",
)
MODEL_ID = "youdub:voxcpm2"


def _control_path() -> Path:
    return runtime_dir() / "videoroll-control.json"


def is_enabled() -> bool:
    try:
        data = json.loads(_control_path().read_text(encoding="utf-8"))
        return data.get("enabled") is True
    except (OSError, ValueError, TypeError):
        return False


def active_jobs(db: Session) -> int:
    # Only actual native jobs block a stop. Historical/paused/terminal rows
    # do not keep the engine in 'busy', while queued jobs cannot be stranded.
    rows = db.query(DubbingJob).filter(DubbingJob.status.in_(BUSY_STATES)).all()
    return sum((row.request_json or {}).get("model_path") == MODEL_ID for row in rows)


def status(db: Session) -> dict:
    ready = model_installed()
    running = active_jobs(db)
    enabled = is_enabled()
    return {
        "engine": MODEL_ID,
        "installed": ready,
        "enabled": enabled,
        "accepting_jobs": ready and enabled,
        "active_jobs": running,
        "busy": running > 0,
        "mode": "on_demand",
        "gpu_loaded": False if running == 0 else None,
        "message": (
            "模型按任务加载，结束后释放显存"
            if ready and enabled else
            "已停止接受新配音任务" if ready else
            "YouDub VoxCPM2 模型或运行环境尚未安装"
        ),
    }


def configure(db: Session, *, enabled: bool) -> dict:
    if enabled and not model_installed():
        raise ValueError("YouDub VoxCPM2 runtime/model is not ready")
    path = _control_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(".videoroll-control.lock")
    with lock.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            db.expire_all()
            if not enabled and active_jobs(db):
                raise RuntimeError(
                    "YouDub has queued/running jobs; pause or finish those tasks before disabling"
                )
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(
                    json.dumps({"enabled": enabled}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    return status(db)
