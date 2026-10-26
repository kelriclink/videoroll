#!/usr/bin/env python3
"""Launch unmodified YouDub PipelineRunner on VideoRoll local video + SRT.

Copies the vendored upstream backend into a persistent runtime directory,
keeping YouDub's own DB, stage checkpoints, audio files and models intact.
Does not implement any replacement TTS or alignment algorithm.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import socketserver
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid
from urllib.parse import quote

SOURCE = Path(__file__).resolve().parents[2] / "vendor" / "youdub_webui"
RUNTIME = Path(os.getenv("VIDEOROLL_YOUDUB_RUNTIME", "/DATA/youdub-runtime")).resolve()


def upstream_version() -> str:
    names = ("backend/app/pipeline.py", "backend/app/adapters/voxcpm.py",
             "backend/app/adapters/demucs.py", "backend/app/adapters/audio.py",
             "submodule/demucs/demucs/api.py")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((SOURCE / name).read_bytes())
    return digest.hexdigest()


def prepare() -> dict:
    """Stage upstream code, but never replace live tasks or their outputs."""
    version_file = RUNTIME / ".source_sha256"
    version = upstream_version()
    if version_file.is_file() and version_file.read_text().strip() == version:
        return {"runtime": str(RUNTIME), "version": version, "changed": False}
    db_path = RUNTIME / "data" / "youdub.sqlite"
    if db_path.is_file():
        with sqlite3.connect(db_path) as db:
            unfinished = db.execute(
                "SELECT count(*) FROM tasks WHERE status IN ('running','queued')"
            ).fetchone()[0]
        if unfinished:
            raise RuntimeError(f"Refusing to update YouDub with {unfinished} active tasks")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for rel in ("backend", "submodule/demucs/demucs", "submodule/demucs/conf"):
        incoming = RUNTIME / (".incoming-" + rel.replace("/", "-"))
        if incoming.exists():
            shutil.rmtree(incoming)
        shutil.copytree(SOURCE / rel, incoming)
        target = RUNTIME / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = RUNTIME / (".previous-" + rel.replace("/", "-"))
        if previous.exists():
            shutil.rmtree(previous)
        if target.exists():
            target.rename(previous)
        incoming.rename(target)
        if previous.exists():
            shutil.rmtree(previous)
    shutil.copyfile(SOURCE / "LICENSE", RUNTIME / "LICENSE")
    version_file.write_text(version + "\n")
    return {"runtime": str(RUNTIME), "version": version, "changed": True}


def load_upstream(*, initialize_db: bool = False):
    """Load the runtime without DDL for status and resume operations.

    Schema initialization writes SQLite even when tables already exist. Doing
    this on every 5s status poll contends with YouDub pipeline's real writes.
    Only creating a new task may initialize the existing schema.
    """
    if not (RUNTIME / "backend/app/pipeline.py").is_file():
        raise RuntimeError("Run --prepare first")
    sys.path.insert(0, str(RUNTIME))
    os.environ.setdefault("WORKFOLDER", str(RUNTIME / "workfolder"))
    os.environ.setdefault("MODEL_CACHE_DIR", str(RUNTIME / "models"))
    from backend.app import config, database, pipeline
    config.ensure_runtime_dirs()
    if initialize_db:
        database.init_db()
    return config, database, pipeline


def create(video: Path, srt: Path, *, direction: str = "en-zh",
           task_id: str | None = None) -> dict:
    if direction not in ("en-zh", "ja-zh", "zh-en"):
        raise ValueError("Unsupported YouDub language direction")
    if not video.is_file() or video.stat().st_size < 1024 or not srt.is_file():
        raise ValueError("Missing or empty video/SRT")
    config, database, _ = load_upstream(initialize_db=True)
    from backend.app.adapters.local_video import uploaded_video_dir
    from backend.app.adapters.local_subtitles import uploaded_subtitle_dir, parse_srt
    from backend.app.youtube import LOCAL_UPLOAD_DIRECTIONS
    if direction not in LOCAL_UPLOAD_DIRECTIONS:
        raise ValueError("Unsupported upstream local direction")
    cues = parse_srt(srt.read_text(encoding="utf-8-sig"))
    task_id = str(uuid.UUID(task_id)) if task_id else str(uuid.uuid4())
    if database.get_task(task_id):
        raise RuntimeError("YouDub task already exists; use --run/--status")
    target_video = uploaded_video_dir(config.WORKFOLDER, task_id) / "source.mp4"
    target_srt = uploaded_subtitle_dir(config.WORKFOLDER, task_id) / "translated.srt"
    target_video.parent.mkdir(parents=True, exist_ok=True)
    target_srt.parent.mkdir(parents=True, exist_ok=True)
    # Upstream runtime_security rejects files with multiple hard links.
    # Make an independent snapshot even when VideoRoll storage shares a disk.
    shutil.copyfile(video, target_video)
    shutil.copyfile(srt, target_srt)
    url = f"local://upload/{task_id}?direction={direction}&filename={quote(video.name)}"
    database.create_task(url, task_id=task_id, output_mode="both", execution_mode="auto")
    database.update_task(task_id, title=video.stem)
    return {"id": task_id, "status": "queued", "segments": len(cues)}


def status(task_id: str) -> dict:
    _, database, _ = load_upstream()
    task_id = str(uuid.UUID(task_id))
    for attempt in range(4):
        try:
            task = database.get_task(task_id)
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 3:
                raise
            # This is an ordinary read; a concurrent SQLite writer may hold
            # the database briefly while finishing a stage checkpoint.
            time.sleep(0.25 * (attempt + 1))
    if not task:
        raise LookupError(f"Unknown YouDub task {task_id}")
    return {
        "id": task_id, "status": task["status"],
        "current_stage": task.get("current_stage"),
        "stages": [{"name": entry["name"], "status": entry["status"],
                    "progress": entry.get("progress"),
                    "error": entry.get("error_message")}
                   for entry in task["stages"]],
        "output_video": task.get("final_video_path"),
        "error": task.get("error_message"),
    }


def run(task_id: str) -> dict:
    _, _, pipeline = load_upstream()
    current = status(task_id)
    if current["status"] not in ("queued", "paused"):
        raise RuntimeError(f"Cannot start {current['status']} YouDub task")
    pipeline.PipelineRunner(str(uuid.UUID(task_id))).run()
    result = status(task_id)
    if result["status"] != "succeeded":
        raise RuntimeError(f"YouDub {result['status']}: {result['error']}")
    output = Path(result["output_video"])
    if not output.is_file() or output.stat().st_size < 1024:
        raise RuntimeError("YouDub did not produce a valid MP4")
    return result


def resume(task_id: str) -> dict:
    _, database, _ = load_upstream()
    prior = status(task_id)
    if prior['status'] != 'failed':
        raise RuntimeError('Only failed YouDub tasks can resume')
    # This is YouDub's own native stage-resume routine: completed stages and
    # valid generated WAVs remain untouched.
    database.reset_failed_for_resume(str(uuid.UUID(task_id)))
    return status(task_id)


def interrupt(task_id: str) -> dict:
    _, database, _ = load_upstream()
    prior = status(task_id)
    if prior['status'] not in ('running', 'queued'):
        raise RuntimeError('Only unfinished native jobs can be marked interrupted')
    # Caller must terminate and wait for PipelineRunner process first; this
    # only reconciles its native stage DB for upstream resume support.
    task_id = str(uuid.UUID(task_id))
    current = prior.get('current_stage')
    if current:
        database.update_stage(task_id, current, status='failed',
                              error_message='Interrupted by VideoRoll controller')
    database.update_task(task_id, status='failed',
                         error_message='Interrupted by VideoRoll controller')
    return status(task_id)


RESIDENT_SOCKET = RUNTIME / "voxcpm-resident.sock"


def resident_request(command: str, task_id: str | None = None, *, timeout: float = 3) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout)
        channel.connect(str(RESIDENT_SOCKET))
        channel.sendall((json.dumps({"command": command, "task_id": task_id}) + "\n").encode())
        if command == "run":
            channel.settimeout(None)  # whole videos may take hours
        body = bytearray()
        while not body.endswith(b"\n"):
            chunk = channel.recv(65536)
            if not chunk:
                raise RuntimeError("Resident VoxCPM2 process disconnected")
            body.extend(chunk)
            if len(body) > 1_000_000:
                raise RuntimeError("Resident VoxCPM2 response exceeded limit")
        result = json.loads(body.decode())
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Resident inference failed"))
        return result.get("result") or {}


def serve_resident() -> None:
    """Reuse upstream VoxCPM2 _MODEL across consecutive, serialized jobs.

    A single-process Unix socket eliminates extra GPU copies. YouDub itself
    remains untouched; opt out of its end-of-stage model eviction only inside
    this dedicated residency process.
    """
    import gc
    from backend.app.adapters import voxcpm
    from backend.app import gpu_memory
    import torch

    config, database, pipeline = load_upstream()
    # Preserve ONLY VoxCPM across tasks. Other YouDub models continue to
    # honor upstream's normal stage cleanup to minimize GPU pressure.
    original_stage_release = gpu_memory.release_stage_memory
    def _release_nonresident_stage(stage: str):
        if stage == "tts":
            return None
        return original_stage_release(stage)
    gpu_memory.release_stage_memory = _release_nonresident_stage
    def _release_nonresident_task():
        return gpu_memory._release_memory(
            "resident task finally", (("backend.app.adapters.whisper_asr", "Whisper"),),
        )
    gpu_memory.release_task_memory = _release_nonresident_task
    voxcpm._load_model()  # real GPU residency; startup fails on OOM
    if RESIDENT_SOCKET.exists():
        try:
            resident_request("ping", timeout=0.5)
        except (OSError, RuntimeError, ValueError):
            RESIDENT_SOCKET.unlink(missing_ok=True)
        else:
            raise RuntimeError("Resident VoxCPM2 already serving")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                message = json.loads(self.rfile.readline(8192))
                action = message.get("command")
                if action == "ping":
                    result = {"gpu_loaded": voxcpm._MODEL is not None, "pid": os.getpid()}
                elif action == "run":
                    result = run(str(uuid.UUID(message["task_id"])))
                    # Clear temporary Torch allocations but retain the model.
                    gc.collect()
                    torch.cuda.empty_cache()
                else:
                    raise ValueError("unsupported resident action")
                self.wfile.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
            except BaseException as exc:
                self.wfile.write((json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}) + "\n").encode())

    class Server(socketserver.UnixStreamServer):
        allow_reuse_address = False

    try:
        with Server(str(RESIDENT_SOCKET), Handler) as server:
            os.chmod(RESIDENT_SOCKET, 0o600)
            print(json.dumps({"status": "resident_ready", "pid": os.getpid()}), flush=True)
            server.serve_forever(poll_interval=1)
    finally:
        RESIDENT_SOCKET.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for value in ("prepare", "create", "run", "status", "resume", "interrupt", "serve"):
        mode.add_argument("--" + value, action="store_true")
    parser.add_argument("--video", type=Path)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--direction", default="en-zh")
    parser.add_argument("--task-id")
    a = parser.parse_args()
    if a.serve:
        load_upstream()  # configure module paths before importing VoxCPM
        serve_resident()
        return
    if a.prepare:
        result = prepare()
    elif a.create:
        if not a.video or not a.srt:
            parser.error("--create requires --video and --srt")
        result = create(a.video.resolve(), a.srt.resolve(),
                        direction=a.direction, task_id=a.task_id)
    elif a.status:
        result = status(a.task_id)
    elif a.resume:
        result = resume(a.task_id)
    elif a.interrupt:
        result = interrupt(a.task_id)
    else:
        if os.getenv("VIDEOROLL_YOUDUB_RESIDENT", "0") == "1":
            result = resident_request("run", a.task_id, timeout=3)
        else:
            result = run(a.task_id)
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
