from __future__ import annotations

import shutil
from pathlib import Path

from videoroll.utils.hf_hub import configure_hf_hub_proxy


FASTER_WHISPER_SIZE_TO_REPO: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}

OPENVINO_WHISPER_SIZE_TO_REPO: dict[str, str] = {
    "tiny": "OpenVINO/whisper-tiny-fp16-ov",
    "base": "OpenVINO/whisper-base-fp16-ov",
    "small": "OpenVINO/whisper-small-fp16-ov",
    "medium": "OpenVINO/whisper-medium-fp16-ov",
    "large-v3": "OpenVINO/whisper-large-v3-fp16-ov",
}

_SUPPORTED_MODEL_DOWNLOAD_ENGINES = {"faster-whisper", "openvino"}


def normalize_model_download_engine(engine: str | None) -> str:
    out = str(engine or "").strip().lower() or "faster-whisper"
    if out not in _SUPPORTED_MODEL_DOWNLOAD_ENGINES:
        raise ValueError(f"unsupported model download engine: {out}")
    return out


def _engine_size_to_repo(engine: str) -> dict[str, str]:
    if engine == "openvino":
        return OPENVINO_WHISPER_SIZE_TO_REPO
    return FASTER_WHISPER_SIZE_TO_REPO


def resolve_model_repo_id(engine: str, model: str) -> str:
    engine_n = normalize_model_download_engine(engine)
    model_s = str(model or "").strip()
    if not model_s:
        raise ValueError("model is required")
    return _engine_size_to_repo(engine_n).get(model_s, model_s)


def default_model_dir_name(engine: str, model: str) -> str:
    engine_n = normalize_model_download_engine(engine)
    model_s = str(model or "").strip()
    if not model_s:
        raise ValueError("model is required")

    if engine_n == "faster-whisper" and model_s in FASTER_WHISPER_SIZE_TO_REPO:
        return model_s

    if engine_n == "openvino" and model_s in OPENVINO_WHISPER_SIZE_TO_REPO:
        return OPENVINO_WHISPER_SIZE_TO_REPO[model_s].split("/", 1)[1]

    return model_s.replace("/", "--")


def download_model_snapshot(
    *,
    engine: str,
    model: str,
    model_dir: Path,
    name: str,
    revision: str | None = None,
    force: bool = False,
    proxy: str | None = None,
) -> Path:
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception as e:
        raise RuntimeError("huggingface_hub is not installed. Rebuild with INSTALL_ASR=1.") from e

    engine_n = normalize_model_download_engine(engine)
    repo_id = resolve_model_repo_id(engine_n, model)
    dest = model_dir / name
    tmp = dest.with_name(dest.name + ".downloading")

    if dest.exists():
        if not force:
            raise RuntimeError("model already exists; set force=true to overwrite")
        shutil.rmtree(dest, ignore_errors=True)

    model_dir.mkdir(parents=True, exist_ok=True)
    configure_hf_hub_proxy(proxy)

    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        try:
            snapshot_download(repo_id=repo_id, revision=revision, local_dir=str(tmp))
        except TypeError:
            snapshot_download(repo_id=repo_id, revision=revision, local_dir=str(tmp))
        shutil.rmtree(dest, ignore_errors=True)
        tmp.replace(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(dest, ignore_errors=True)
        raise

    return dest
