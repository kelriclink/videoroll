"""Allowlisted on-disk Qwen3-TTS model management."""
from __future__ import annotations
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

MODELS = ("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
TOKENIZER = "Qwen/Qwen3-TTS-Tokenizer-12Hz"
VOICES = ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric")
DEVICES = ("cuda:0", "cuda:1", "cpu")
SOURCES = ("huggingface", "modelscope")


def root() -> Path:
    result = Path(os.getenv("DUBBING_MODEL_ROOT") or Path(os.getenv("VIDEOROLL_DATA_ROOT", "/opt/videoroll/data")) / "models" / "tts")
    if not result.is_absolute() or result == Path("/"):
        raise ValueError("invalid model directory")
    return result


def folder(model: str, base: Path | None = None) -> Path:
    if model not in MODELS:
        raise ValueError("unsupported TTS model")
    return (base or root()) / model.split("/")[-1]


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


@contextmanager
def lock(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    with (base / ".model-management.lock").open("a+b") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def installed(model: str, base: Path | None = None) -> bool:
    d = folder(model, base)
    tokenizer = (base or root()) / TOKENIZER.split("/")[-1]
    return ((tokenizer / ".videoroll-complete.json").is_file()
            and (d / ".videoroll-complete.json").is_file() and (d / "config.json").is_file()
            and any(x.is_file() and x.stat().st_size > 1024 for x in d.rglob("*.safetensors")))


def selection(base: Path | None = None) -> dict:
    value = read_json((base or root()) / "selected.json")
    return {
        "model": value.get("model") if value.get("model") in MODELS else MODELS[0],
        "voice": value.get("voice") if value.get("voice") in VOICES else "Vivian",
        "device": value.get("device") if value.get("device") in DEVICES else "cuda:0",
    }


def choose(model: str, voice: str, device: str, base: Path | None = None) -> dict:
    base = base or root()
    if model not in MODELS or voice not in VOICES or device not in DEVICES:
        raise ValueError("invalid model, speaker or device")
    if not installed(model, base):
        raise ValueError("download the model before choosing it")
    with lock(base):
        data = {"model": model, "voice": voice, "device": device}
        atomic_json(base / "selected.json", data)
    return data


def state_file(model: str, base: Path) -> Path:
    return base / ("." + folder(model, base).name + ".download.json")


def status(model: str, base: Path | None = None) -> dict:
    base = base or root()
    data = read_json(state_file(model, base))
    state = str(data.get("status") or "not_downloaded")
    pid = data.get("pid")
    if state in ("queued", "downloading") and isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            state = "interrupted"
        except PermissionError:
            pass
    done = installed(model, base)
    if done and state not in ("queued", "downloading"):
        state = "installed"
    elif not done and state == "installed":
        state = "interrupted"
    size = 0
    d = folder(model, base)
    if d.is_dir():
        for x in d.rglob("*"):
            try:
                if x.is_file() and not x.is_symlink():
                    size += x.stat().st_size
            except OSError:
                pass
    return {"model": model, "installed": done, "status": state, "bytes": size,
            "message": str(data.get("message") or ""), "source": str(data.get("source") or "huggingface")}


def inventory(base: Path | None = None) -> dict:
    base = base or root()
    return {"models": [status(m, base) for m in MODELS], "selection": selection(base),
            "model_root": str(base), "voices": list(VOICES), "devices": list(DEVICES),
            "sources": list(SOURCES),
            "tokenizer_installed": (base / TOKENIZER.split("/")[-1] / ".videoroll-complete.json").is_file()}


def download(model: str, source: str, base: Path | None = None, *, python: str | None = None) -> dict:
    base = base or root()
    if model not in MODELS or source not in SOURCES:
        raise ValueError("unsupported model or source")
    with lock(base):
        before = status(model, base)
        if before["installed"]:
            return before
        if before["status"] in ("queued", "downloading"):
            raise ValueError("this model is already downloading")
        interpreter = python or os.getenv("DUBBING_DOWNLOAD_PYTHON") or sys.executable
        if not Path(interpreter).is_file():
            raise RuntimeError("TTS model downloader is not installed")
        sf = state_file(model, base)
        atomic_json(sf, {"status": "queued", "source": source, "message": "Starting downloader"})
        try:
            child = subprocess.Popen(
                [interpreter, "-m", "videoroll.apps.dubbing.model_downloader",
                 "--model", model, "--source", source, "--root", str(base)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        except Exception:
            sf.unlink(missing_ok=True)
            raise
        atomic_json(sf, {"status": "downloading", "pid": child.pid,
                         "source": source, "message": "Downloading model"})
    return status(model, base)
