"""Persistent VideoRoll control plane for upstream YouDub VoxCPM2.

The engine is launched on demand inside the existing dubbing workflow worker,
so an enabled engine does NOT permanently occupy VRAM. Disabling blocks new
jobs and never kills a live YouDub task.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import time
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


def _config() -> dict:
    try:
        return json.loads(_control_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def is_resident() -> bool:
    config = _config()
    return bool(config.get("enabled") and config.get("resident"))


def _socket_path() -> Path:
    return runtime_dir() / "voxcpm-resident.sock"


def _pid_path() -> Path:
    return runtime_dir() / "voxcpm-resident.pid"


def resident_alive() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(0.4)
            conn.connect(str(_socket_path()))
            conn.sendall(b'{"command":"ping"}\n')
            data = conn.recv(8192)
            return bool(json.loads(data).get("result", {}).get("gpu_loaded"))
    except (OSError, ValueError, TypeError):
        return False


def _free_gpu_megabytes() -> int:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True, timeout=8,
    )
    return int(result.stdout.strip().splitlines()[0].strip())


def _resident_pid_running() -> bool:
    from videoroll.apps.dubbing.youdub_native import bridge_path
    try:
        pid = int(_pid_path().read_text().strip())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        return str(bridge_path()) in command and "--serve" in command
    except (OSError, ValueError):
        return False


def _spawn_resident() -> None:
    from videoroll.apps.dubbing.youdub_native import native_python, bridge_path
    if resident_alive() or _resident_pid_running():
        return
    free = _free_gpu_megabytes()
    if free < 12_000:
        raise RuntimeError(
            f"V100 available memory {free} MiB; residency requires at least 12000 MiB "
            "free before starting. Disable competing models or keep on-demand mode."
        )
    log_path = runtime_dir() / "voxcpm-resident.log"
    with log_path.open("ab") as stream:
        proc = subprocess.Popen(
            [str(native_python()), str(bridge_path()), "--serve"],
            stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True, stdin=subprocess.DEVNULL,
            env={**os.environ, "VOXCPM_OPTIMIZE": "false",
                 "VOXCPM_MODEL_DIR": str(runtime_dir() / "models/OpenBMB__VoxCPM2-modelscope")},
        )
    _pid_path().write_text(str(proc.pid) + "\n")


def stop_resident() -> None:
    from videoroll.apps.dubbing.youdub_native import bridge_path
    pid_path = _pid_path()
    try:
        pid = int(pid_path.read_text().strip())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        if str(bridge_path()) in command and "--serve" in command:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _resident_pid_running():
                    break
                time.sleep(0.1)
            if _resident_pid_running():
                os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError, ProcessLookupError):
        pass
    finally:
        pid_path.unlink(missing_ok=True)
        _socket_path().unlink(missing_ok=True)


def ensure_resident(progress=None, should_continue=None) -> None:
    if not is_resident():
        return
    if resident_alive():
        return
    # Starts lazily after a prior crash; only VideoRoll GPU slot holder calls this.
    _spawn_resident()
    for _ in range(45):  # 180 sec for initial model load
        if resident_alive():
            return
        if should_continue:
            should_continue()
        if progress:
            progress({"current_stage": "loading_model", "stages": [], "status": "queued"})
        time.sleep(4)
    raise RuntimeError("Resident VoxCPM2 did not become ready within 180 seconds")


def is_enabled() -> bool:
    return _config().get("enabled") is True


def active_jobs(db: Session) -> int:
    # Only actual native jobs block a stop. Historical/paused/terminal rows
    # do not keep the engine in 'busy', while queued jobs cannot be stranded.
    rows = db.query(DubbingJob).filter(DubbingJob.status.in_(BUSY_STATES)).all()
    return sum((row.request_json or {}).get("model_path") == MODEL_ID for row in rows)


def status(db: Session) -> dict:
    ready = model_installed()
    running = active_jobs(db)
    enabled = is_enabled()
    resident = is_resident()
    loaded = resident_alive() if resident and running == 0 else (None if running else False)
    return {
        "engine": MODEL_ID,
        "installed": ready,
        "enabled": enabled,
        "resident": resident,
        "accepting_jobs": ready and enabled,
        "active_jobs": running,
        "busy": running > 0,
        "mode": "resident" if resident else "on_demand",
        "gpu_loaded": loaded,
        "message": (
            "模型已驻留在 GPU，连续任务复用模型" if loaded else
            "驻留进程正在加载模型；请等待显存状态更新" if resident else
            "模型按任务加载，结束后释放显存" if ready and enabled else
            "已停止接受新配音任务" if ready else
            "YouDub VoxCPM2 模型或运行环境尚未安装"
        ),
    }


def configure(db: Session, *, enabled: bool, resident: bool | None = None) -> dict:
    if enabled and not model_installed():
        raise ValueError("YouDub VoxCPM2 runtime/model is not ready")
    path = _control_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(".videoroll-control.lock")
    with lock.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            old = _config()
            target_resident = bool(resident if resident is not None else old.get("resident")) and enabled
            changed_mode = target_resident != bool(old.get("resident"))
            db.expire_all()
            if (not enabled or changed_mode) and active_jobs(db):
                raise RuntimeError(
                    "queued/running YouDub jobs must finish before disabling or changing model residency"
                )
            if target_resident and not old.get("resident") and not resident_alive():
                # Reject switching before saving settings if GPU is already crowded.
                free = _free_gpu_megabytes()
                if free < 12_000:
                    raise RuntimeError(f"GPU free VRAM is only {free} MiB; residence needs >=12000 MiB")
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(
                    json.dumps({"enabled": enabled, "resident": target_resident}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            if not target_resident:
                stop_resident()
            elif not resident_alive() and not _resident_pid_running():
                _spawn_resident()  # nonblocking initial model load
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    return status(db)
