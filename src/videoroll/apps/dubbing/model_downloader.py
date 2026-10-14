"""Download Qwen3-TTS weights in a detached process with durable status."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from videoroll.apps.dubbing.model_management import (MODELS, COSY_MODELS, COSY_REPO,
    COSY_FOLDER, SOURCES, TOKENIZER, atomic_json, state_file)


def snapshot(repo: str, destination: Path, source: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if source == "huggingface":
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=repo, repo_type="model", local_dir=str(destination))
    else:
        from modelscope import snapshot_download
        snapshot_download(model_id=repo, local_dir=str(destination))


def perform(model: str, source: str, root: Path) -> None:
    if model not in MODELS or source not in SOURCES:
        raise ValueError("model/source not allowed")
    root.mkdir(parents=True, exist_ok=True)
    state = state_file(model, root)
    pid = os.getpid()
    try:
        for repo in ((COSY_REPO,) if model in COSY_MODELS else (TOKENIZER, model)):
            directory = root / repo.split("/")[-1]
            marker = directory / ".videoroll-complete.json"
            if model in COSY_MODELS:
                required = ("cosyvoice3.yaml", "llm.pt", "llm.rl.pt", "flow.pt", "hift.pt",
                            "campplus.onnx", "speech_tokenizer_v3.onnx",
                            "CosyVoice-BlankEN/model.safetensors")
                valid = all((directory / f).is_file() and (directory / f).stat().st_size > 1024
                            for f in required)
            else:
                valid = ((directory / "config.json").is_file()
                         and any(x.is_file() and x.stat().st_size > 1024
                                 for x in directory.rglob("*.safetensors")))
            if marker.is_file() and valid:
                continue
            marker.unlink(missing_ok=True)
            atomic_json(state, {"pid": pid, "status": "downloading", "source": source,
                                "message": f"Downloading {repo.split('/')[-1]}"})
            snapshot(repo, directory, source)
            if model in COSY_MODELS:
                if not all((directory / f).is_file() and (directory / f).stat().st_size > 1024
                           for f in required):
                    raise RuntimeError(f"{repo} download incomplete: CosyVoice3 components missing")
            else:
                if not (directory / "config.json").is_file():
                    raise RuntimeError(f"{repo} download incomplete: no config.json")
                if not any(x.is_file() and x.stat().st_size > 1024 for x in directory.rglob("*.safetensors")):
                    raise RuntimeError(f"{repo} download incomplete: no safetensors")
            atomic_json(marker, {"repo": repo, "source": source, "completed_at": time.time()})
        atomic_json(state, {"pid": pid, "status": "installed", "source": source,
                            "message": "Model and tokenizer installed"})
    except Exception as exc:
        atomic_json(state, {"pid": pid, "status": "failed", "source": source,
                            "message": f"{type(exc).__name__}: {str(exc)[:600]}"})
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        perform(args.model, args.source, args.root)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
