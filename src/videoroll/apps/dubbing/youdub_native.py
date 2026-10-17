"""VideoRoll orchestration adapter for the *unmodified* native YouDub engine.

No local TTS, voice-selection, time stretching, mixing or video mux logic.
All those stages execute inside upstream YouDub PipelineRunner subprocess.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


DEFAULT_RUNTIME = Path("/DATA/youdub-runtime")
STAGES = ("download", "separate", "asr", "asr_fix", "translate",
          "split_audio", "tts", "merge_audio", "merge_video")


@dataclass(frozen=True)
class NativeResult:
    video: Path
    audio: Path
    stages: tuple[dict, ...]
    mixed_audio: Path | None = None
    aligned_srt: Path | None = None


def bridge_path() -> Path:
    return Path(__file__).resolve().parents[4] / "scripts/native/youdub_bridge.py"


def runtime_dir() -> Path:
    return Path(os.getenv("VIDEOROLL_YOUDUB_RUNTIME", str(DEFAULT_RUNTIME)))


def native_python() -> Path:
    return Path(os.getenv(
        "VIDEOROLL_YOUDUB_PYTHON",
        str(runtime_dir() / "venv/bin/python"),
    ))


def model_installed() -> bool:
    root = runtime_dir()
    model = Path(os.getenv(
        "VOXCPM_MODEL_DIR",
        str(root / "models/OpenBMB__VoxCPM2-modelscope"),
    ))
    return (
        (root / "backend/app/pipeline.py").is_file()
        and (root / "submodule/demucs/demucs/api.py").is_file()
        and native_python().is_file()
        and (model / "model.safetensors").is_file()
        and (model / "model.safetensors").stat().st_size > 1_000_000_000
        and (model / "audiovae.pth").is_file()
    )


def _srt_timestamp(seconds: float) -> str:
    ms = round(float(seconds) * 1000)
    if ms < 0:
        raise ValueError("subtitle timestamps cannot be negative")
    hh, rem = divmod(ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, rem = divmod(rem, 1_000)
    return f"{hh:02}:{mm:02}:{ss:02},{rem:03}"


def convert_subtitle_json(raw: list[dict], srt: Path) -> int:
    if not raw or len(raw) > 50_000:
        raise ValueError("Translated subtitle array is empty or too large")
    blocks = []
    previous_start = -1
    for idx, item in enumerate(raw, 1):
        start = float(item["start"])
        end = float(item["end"])
        caption = str(item.get("text") or "").strip()
        if not caption or end <= start or start + 1e-6 < previous_start:
            raise ValueError(f"Invalid translated caption {idx}")
        if any(c == "\x00" for c in caption):
            raise ValueError(f"Invalid control character in caption {idx}")
        previous_start = start
        blocks.append(
            f"{idx}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{caption}"
        )
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return len(blocks)


def _cli(*args: str) -> dict:
    python = native_python()
    bridge = bridge_path()
    if not python.is_file() or not bridge.is_file():
        raise RuntimeError("Native YouDub environment or bridge is missing")
    env = os.environ.copy()
    env.setdefault(
        "VOXCPM_MODEL_DIR",
        str(runtime_dir() / "models/OpenBMB__VoxCPM2-modelscope"),
    )
    process = subprocess.run(
        [str(python), str(bridge), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"YouDub native bridge failed: {(process.stderr or process.stdout)[-1000:]}"
        )
    try:
        return json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("YouDub bridge returned invalid JSON") from exc


def _poll_native_status(job_id: str, progress: Callable[[dict], None]) -> bool:
    """Best-effort state sample; a busy SQLite reader must not stop TTS.

    Only sqlite transient contention is soft. Any other status/bridge error
    still propagates so the existing worker can enforce process cleanup.
    """
    try:
        state = _cli("--status", "--task-id", str(job_id))
    except RuntimeError as exc:
        if "database is locked" not in str(exc).lower():
            raise
        return False
    progress(state)
    return True


def _stop_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


@contextmanager
def _exclusive_gpu_slot(progress: Callable[[dict], None], should_continue: Callable[[], None]):
    """Host-level guard: even another Hatchet worker/process cannot run a
    second Demucs/VoxCPM task on the same V100 simultaneously. The wait
    heartbeats the DB lease and remains cancelable.
    """
    lock_path = runtime_dir() / ".videoroll-native-gpu.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        while True:
            should_continue()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                progress({"current_stage": "waiting_gpu", "stages": [], "status": "queued"})
                time.sleep(5)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _run_native_unlocked(
    *, job_id: uuid.UUID, video: Path, raw_subtitles: Path,
    workspace: Path,
    progress: Callable[[dict], None],
    should_continue: Callable[[], None],
) -> NativeResult:
    """Call upstream YouDub create/resume/run, polling its own stage database."""
    if not model_installed():
        raise RuntimeError("Native YouDub VoxCPM2 model/runtime is not ready")
    from videoroll.apps.dubbing.youdub_control import is_enabled
    if not is_enabled():
        raise RuntimeError("YouDub is disabled in VideoRoll settings")
    workspace.mkdir(parents=True, exist_ok=True)
    srt = workspace / "translated.srt"
    raw = json.loads(raw_subtitles.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Source subtitles must be JSON list")
    convert_subtitle_json(raw, srt)
    try:
        state = _cli("--status", "--task-id", str(job_id))
    except RuntimeError as exc:
        if "Unknown YouDub task" not in str(exc):
            raise
        state = _cli("--create", "--task-id", str(job_id),
                     "--video", str(video), "--srt", str(srt))
        # The bridge stays upstream-compatible; enable per-task audio-only
        # processing with a marker only for newly created tasks. Older jobs
        # never receive the flag, including after restart/resume.
        if os.getenv("VIDEOROLL_YOUDUB_AUDIO_ONLY", "1").strip().lower() not in {"0", "false", "off", "no"}:
            upload_root = Path(os.getenv("WORKFOLDER", str(runtime_dir() / "workfolder"))) / "_uploads" / str(job_id)
            (upload_root / "audio_only.flag").write_text("1\n", encoding="ascii")
    if state["status"] == "running":
        # Our DB lease is now owned by this worker; reconcile a native runner
        # whose process exited before updating YouDub's own status.
        state = _cli("--interrupt", "--task-id", str(job_id))
    if state["status"] == "failed":
        state = _cli("--resume", "--task-id", str(job_id))
    if state["status"] == "succeeded":
        # Resuming a finished native job should only re-import the already
        # completed media into VideoRoll, never regenerate a single sentence.
        pass
    elif state["status"] != "queued":
        raise RuntimeError(f"YouDub task cannot resume from {state['status']}")
    else:
        from videoroll.apps.dubbing.youdub_control import (
            is_resident, ensure_resident, stop_resident,
        )
        resident = is_resident()
        if resident:
            ensure_resident(progress=progress, should_continue=should_continue)
        env = os.environ.copy()
        env["VIDEOROLL_YOUDUB_RESIDENT"] = "1" if resident else "0"
        env.setdefault(
            "VOXCPM_MODEL_DIR",
            str(runtime_dir() / "models/OpenBMB__VoxCPM2-modelscope"),
        )
        log_path = workspace / "youdub-native.log"
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                [str(native_python()), str(bridge_path()), "--run",
                 "--task-id", str(job_id)],
                stdout=log, stderr=subprocess.STDOUT, env=env,
                start_new_session=True,
            )
            try:
                while proc.poll() is None:
                    should_continue()
                    # A transient SQLite lock must never terminate an
                    # otherwise healthy Demucs or VoxCPM2 subprocess.
                    _poll_native_status(str(job_id), progress)
                    time.sleep(5)
            except BaseException:
                _stop_group(proc)
                # In resident mode the socket client is only a proxy; the
                # actual YouDub PipelineRunner must be killed too or it keeps
                # running after the VideoRoll task was paused/canceled.
                if resident:
                    stop_resident()
                # Upstream only resumes jobs marked failed. Do not leave a
                # killed native runner with its stage stuck in 'running'.
                try:
                    _cli('--interrupt', '--task-id', str(job_id))
                except RuntimeError:
                    pass  # Preserve the primary cancellation/lease error.
                raise
            if proc.returncode != 0:
                state = _cli("--status", "--task-id", str(job_id))
                raise RuntimeError(
                    f"Native YouDub failed at {state.get('current_stage')}: "
                    f"{state.get('error')}; see {log_path}"
                )
    state = _cli("--status", "--task-id", str(job_id))
    if state["status"] != "succeeded":
        raise RuntimeError(f"Native YouDub did not complete: {state}")
    output = Path(state["output_video"])
    if not output.is_file() or output.stat().st_size < 1024:
        raise RuntimeError("YouDub output artifact is missing")
    session = output.parents[1]
    audio = session / "tmp" / "audio_dubbing.wav"
    if not audio.is_file() or audio.stat().st_size < 1024:
        raise RuntimeError("YouDub generated speech track is missing")
    info_path = session / "metadata" / "local_info.json"
    is_audio_only = bool(json.loads(info_path.read_text(encoding="utf-8")).get("audio_only")) if info_path.is_file() else False
    if is_audio_only:
        mixed = session / "tmp" / "audio_mixed.m4a"
        subtitles = next((path for path in sorted((session / "metadata").glob("subtitles.*.srt"))
                          if path.is_file()), None)
        if not mixed.is_file() or mixed.stat().st_size < 1024 or subtitles is None:
            raise RuntimeError("YouDub audio-only handoff is missing mixed audio or aligned subtitles")
        return NativeResult(video=output, audio=audio, stages=tuple(state["stages"]),
                            mixed_audio=mixed, aligned_srt=subtitles)
    return NativeResult(video=output, audio=audio, stages=tuple(state["stages"]))


def run_native(
    *, job_id: uuid.UUID, video: Path, raw_subtitles: Path,
    workspace: Path,
    progress: Callable[[dict], None],
    should_continue: Callable[[], None],
) -> NativeResult:
    with _exclusive_gpu_slot(progress, should_continue):
        return _run_native_unlocked(
            job_id=job_id, video=video, raw_subtitles=raw_subtitles,
            workspace=workspace, progress=progress,
            should_continue=should_continue,
        )
