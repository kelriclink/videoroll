from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from videoroll.ai.client import OpenAIChatConfig, request_openai_embedding
from videoroll.apps.subtitle_service.model_downloads import download_model_snapshot


_SAFE_MODEL_NAME_REPLACEMENTS = {"/": "--", "\\": "--"}
_LOCAL_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_LOCAL_MODEL_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str
    model: str
    dimensions: int
    model_dir: str
    device: str
    openai_config: OpenAIChatConfig


def normalize_embedding_provider(provider: str | None) -> str:
    out = str(provider or "").strip().lower() or "openai"
    if out not in {"openai", "local"}:
        raise ValueError(f"unsupported embedding provider: {out}")
    return out


def safe_embedding_model_name(model: str) -> str:
    out = str(model or "").strip()
    if not out:
        raise ValueError("model is required")
    for old, new in _SAFE_MODEL_NAME_REPLACEMENTS.items():
        out = out.replace(old, new)
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in out)
    return out[:96] or "embedding-model"


def embedding_model_path(model_dir: str | Path, model: str) -> Path:
    p = Path(model_dir) / safe_embedding_model_name(model)
    return p


def list_local_embedding_models(model_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(model_dir)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        size = 0
        for item in p.rglob("*"):
            if item.is_file():
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
        out.append({"name": p.name, "path": str(p), "size_bytes": size})
    return out


def download_local_embedding_model(
    *,
    model: str,
    model_dir: str | Path,
    name: str | None = None,
    revision: str | None = None,
    force: bool = False,
    proxy: str | None = None,
) -> Path:
    model_name = str(model or "").strip()
    if not model_name:
        raise ValueError("model is required")
    dest_name = safe_embedding_model_name(name or model_name)
    return download_model_snapshot(
        engine="embedding",
        model=model_name,
        model_dir=Path(model_dir),
        name=dest_name,
        revision=revision,
        force=force,
        proxy=proxy,
    )


def delete_local_embedding_model(*, model_dir: str | Path, name: str) -> None:
    path = Path(model_dir) / safe_embedding_model_name(name)
    if path.exists():
        shutil.rmtree(path)


def _load_sentence_transformer(path: Path, *, device: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as e:
        raise RuntimeError("sentence-transformers is not installed. Rebuild the app image with embedding dependencies.") from e
    if not path.exists():
        raise RuntimeError(f"local embedding model is not downloaded: {path}")
    device_arg = str(device or "cpu").strip() or "cpu"
    cache_key = ("sentence-transformers", str(path), device_arg)
    with _LOCAL_MODEL_CACHE_LOCK:
        model = _LOCAL_MODEL_CACHE.get(cache_key)
        if model is None:
            model = SentenceTransformer(str(path), device=device_arg)
            _LOCAL_MODEL_CACHE[cache_key] = model
        return model


def _parse_local_device(device: str) -> tuple[str, str]:
    value = str(device or "cpu").strip() or "cpu"
    lowered = value.lower()
    if lowered.startswith("openvino"):
        _, sep, raw_device = value.partition(":")
        ov_device = raw_device.strip() if sep else "CPU"
        return "openvino", ov_device or "CPU"
    return "sentence-transformers", value


def _openvino_export_dir(path: Path) -> Path:
    if (path / "openvino_model.xml").exists():
        return path
    return path / "openvino"


def _load_openvino_feature_extractor(path: Path, *, device: str) -> tuple[Any, Any]:
    try:
        from optimum.intel.openvino import OVModelForFeatureExtraction  # type: ignore
        from transformers import AutoTokenizer  # type: ignore
    except Exception as e:
        raise RuntimeError("OpenVINO embedding requires optimum-intel[openvino]. Rebuild the app image with embedding dependencies.") from e
    if not path.exists():
        raise RuntimeError(f"local embedding model is not downloaded: {path}")

    device_arg = str(device or "CPU").strip() or "CPU"
    ov_dir = _openvino_export_dir(path)
    cache_key = ("openvino-feature-extraction", str(ov_dir), device_arg)
    with _LOCAL_MODEL_CACHE_LOCK:
        cached = _LOCAL_MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        if (ov_dir / "openvino_model.xml").exists():
            model = OVModelForFeatureExtraction.from_pretrained(str(ov_dir), device=device_arg)
            tokenizer = AutoTokenizer.from_pretrained(str(path))
        else:
            ov_dir.mkdir(parents=True, exist_ok=True)
            model = OVModelForFeatureExtraction.from_pretrained(str(path), export=True, device=device_arg)
            tokenizer = AutoTokenizer.from_pretrained(str(path))
            try:
                model.save_pretrained(str(ov_dir))
                tokenizer.save_pretrained(str(ov_dir))
            except Exception:
                pass

        cached = (model, tokenizer)
        _LOCAL_MODEL_CACHE[cache_key] = cached
        return cached


def _mean_pool_embedding(model_output: Any, attention_mask: Any) -> list[float]:
    try:
        import numpy as np  # type: ignore
        import torch  # type: ignore
    except Exception as e:
        raise RuntimeError("OpenVINO embedding pooling requires numpy and torch.") from e

    if isinstance(model_output, dict):
        token_embeddings = model_output.get("last_hidden_state")
    else:
        token_embeddings = getattr(model_output, "last_hidden_state", None)
        if token_embeddings is None and isinstance(model_output, (list, tuple)) and model_output:
            token_embeddings = model_output[0]
    if token_embeddings is None:
        raise RuntimeError("OpenVINO embedding output missing last_hidden_state")

    if hasattr(token_embeddings, "detach"):
        token_embeddings_np = token_embeddings.detach().cpu().numpy()
    else:
        token_embeddings_np = np.asarray(token_embeddings)

    if hasattr(attention_mask, "detach"):
        mask_np = attention_mask.detach().cpu().numpy()
    else:
        mask_np = np.asarray(attention_mask)

    mask_np = np.expand_dims(mask_np, axis=-1).astype(np.float32)
    summed = (token_embeddings_np * mask_np).sum(axis=1)
    counts = np.clip(mask_np.sum(axis=1), 1e-9, None)
    vector = summed / counts
    tensor = torch.tensor(vector, dtype=torch.float32)
    tensor = torch.nn.functional.normalize(tensor, p=2, dim=1)
    return _normalize_vector(tensor.detach().cpu().numpy())


def _embed_text_openvino(path: Path, text: str, *, device: str) -> list[float]:
    model, tokenizer = _load_openvino_feature_extractor(path, device=device)
    inputs = tokenizer(text, padding=True, truncation=True, return_tensors="pt")
    outputs = model(**inputs)
    return _mean_pool_embedding(outputs, inputs["attention_mask"])


def _normalize_vector(values: Any) -> list[float]:
    try:
        import numpy as np  # type: ignore

        if isinstance(values, np.ndarray):
            raw = values.tolist()
        else:
            raw = values
    except Exception:
        raw = values

    if isinstance(raw, list) and raw and isinstance(raw[0], list):
        raw = raw[0]
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("embedding output is empty")
    return [float(x) for x in raw]


def embed_text(text: str, *, settings: EmbeddingSettings) -> list[float]:
    provider = normalize_embedding_provider(settings.provider)
    source = str(text or "").strip()
    if not source:
        raise ValueError("embedding text is empty")

    if provider == "openai":
        return request_openai_embedding(config=settings.openai_config, text=source)

    model_path = embedding_model_path(settings.model_dir, settings.model)
    backend, device = _parse_local_device(settings.device)
    if backend == "openvino":
        return _embed_text_openvino(model_path, source, device=device)

    model = _load_sentence_transformer(model_path, device=device)
    try:
        encoded = model.encode(source, normalize_embeddings=True)
    except TypeError:
        encoded = model.encode(source)
    return _normalize_vector(encoded)


def assert_embedding_dimensions(vector: list[float], expected: int) -> None:
    if expected <= 0:
        return
    if len(vector) != expected:
        raise RuntimeError(f"embedding dimension mismatch: expected {expected}, got {len(vector)}")


def embedding_settings_from_translate_settings(settings: dict[str, Any]) -> EmbeddingSettings:
    provider = normalize_embedding_provider(str(settings.get("rag_embedding_provider") or "openai"))
    return EmbeddingSettings(
        provider=provider,
        model=str(settings.get("rag_embedding_model") or "text-embedding-3-small").strip() or "text-embedding-3-small",
        dimensions=max(1, min(4096, int(settings.get("rag_embedding_dimensions") or 1536))),
        model_dir=str(settings.get("rag_embedding_model_dir") or "/models/embeddings").strip() or "/models/embeddings",
        device=str(settings.get("rag_embedding_device") or "cpu").strip() or "cpu",
        openai_config=OpenAIChatConfig(
            api_key=str(settings.get("openai_api_key") or "").strip() or None,
            base_url=str(settings.get("openai_base_url") or "").strip(),
            model=str(settings.get("rag_embedding_model") or "text-embedding-3-small").strip() or "text-embedding-3-small",
            temperature=0.0,
            timeout_seconds=float(settings.get("openai_timeout_seconds") or 60.0),
        ),
    )
