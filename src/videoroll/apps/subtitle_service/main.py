from __future__ import annotations

from contextlib import asynccontextmanager

import csv
import io
import json

import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import wave
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Generator
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError

from videoroll.ai.service import AIService
from videoroll.apps.security.service_auth import install_internal_service_auth, service_token
from videoroll.config import SubtitleServiceSettings, get_subtitle_settings
from videoroll.db.migrate import initialize_database
from videoroll.db.models import Asset, AssetKind, RenderJob, RenderJobStatus, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.db.session import db_session
from videoroll.storage.filesystem import FileStore
from videoroll.apps.subtitle_service.schemas import (
    ASRDefaultsRead,
    ASRDefaultsUpdate,
    CloudflareWorkersAITestRequest,
    CloudflareWorkersAITestResponse,
    ExternalWhisperTestRequest,
    ExternalWhisperTestResponse,
    GroqWhisperTestRequest,
    GroqWhisperTestResponse,
    IntelHardwareProbeRead,
    SubtitleJobCreate,
    SubtitleJobRead,
    SubtitleAutoProfileRead,
    SubtitleAutoProfileUpdate,
    TranslateSettingsRead,
    TranslateSettingsUpdate,
    TranslateTestRequest,
    TranslateTestResponse,
    WhisperModelDownloadRequest,
    WhisperModelInfo,
    WhisperSettingsRead,
    ModelDownloadProxyTestRequest,
    ModelDownloadProxyTestResponse,
    KnowledgeItemRead,
    KnowledgeEmbeddingRebuildRequest,
    KnowledgeEmbeddingRebuildResponse,
    KnowledgeBulkImportResponse,
    KnowledgeItemUpsertRequest,
    KnowledgeItemUpsertResponse,
    DictionaryEntryRead,
    DictionaryEntryUpdate,
    DictionaryImportResponse,
    DictionaryLookupRequest,
    DictionaryLookupResponse,
    DictionaryPromoteRequest,
    DictionaryPromoteResponse,
    DictionarySourceRead,
    DictionarySourceUpdate,
    AgentRunRead,
    AgentSkillRead,
    EmbeddingModelDownloadRequest,
    EmbeddingModelListRequest,
    EmbeddingModelInfo,
    EmbeddingTestRequest,
    EmbeddingTestResponse,
    EmbeddingRuntimeStatusRead,
    TaskQueueItemRead,
    TaskQueueRead,
    TaskQueueSettingsRead,
)
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings, update_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile, update_auto_profile
from videoroll.apps.subtitle_service.model_downloads import (
    default_model_dir_name,
    download_model_snapshot,
    normalize_model_download_engine,
)
from videoroll.apps.subtitle_service.embeddings import (
    assert_embedding_dimensions,
    delete_local_embedding_model,
    download_local_embedding_model,
    embedding_settings_from_translate_settings,
    embed_text,
    list_local_embedding_models,
)
from videoroll.apps.subtitle_service.rag import (
    build_knowledge_embedding_text,
    delete_knowledge_item,
    get_agent_run,
    list_agent_runs,
    load_agent_skill_registry,
    rebuild_knowledge_embeddings,
    list_knowledge_items,
    rag_settings_from_translate_settings,
    upsert_knowledge_item,
)
from videoroll.apps.subtitle_service.dictionaries import (
    delete_dictionary_entry,
    delete_dictionary_source,
    dictionary_import_presets,
    get_dictionary_entry,
    get_dictionary_import_preset,
    import_dictionary_file,
    list_dictionary_entries,
    list_dictionary_sources,
    lookup_dictionary_entries,
    set_dictionary_entry_enabled,
    update_dictionary_source,
)
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings, update_translate_settings
from videoroll.apps.subtitle_service.processing import (
    transcribe_cloudflare_workers_ai,
    transcribe_external_whisper,
    transcribe_groq_whisper,
)
from videoroll.utils.auto_youtube import parse_auto_youtube_created_by
from videoroll.utils.cpu import process_cpu_count
from videoroll.utils.httpx_proxy import HTTPX_PROXY_KWARG_UNSUPPORTED, format_httpx_proxy_error
from videoroll.utils.intel_gpu import detect_intel_hardware
from videoroll.realtime import publish_queue_changed

logger = logging.getLogger(__name__)
_WHISPER_MODEL_ZIP_MAX_BYTES = 20 * 1024 * 1024 * 1024
_WHISPER_MODEL_ZIP_MAX_FILES = 100_000
_WHISPER_MODEL_ZIP_MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024 * 1024


def get_settings() -> SubtitleServiceSettings:
    return get_subtitle_settings()


def get_db(settings: SubtitleServiceSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


def _models_dir(settings: SubtitleServiceSettings) -> Path:
    return Path(settings.whisper_model_dir)


def _embedding_models_dir(settings: SubtitleServiceSettings) -> Path:
    return Path(settings.rag_embedding_model_dir)


def _embedding_models_dir_from_translate_settings(cfg: dict[str, Any], settings: SubtitleServiceSettings) -> Path:
    path = str(cfg.get("rag_embedding_model_dir") or "").strip()
    if path:
        return Path(path)
    return _embedding_models_dir(settings)


def _dictionary_imports_dir(settings: SubtitleServiceSettings) -> Path:
    return Path(getattr(settings, "dictionary_data_dir", "data/dictionaries") or "data/dictionaries") / "imports"


def _safe_upload_filename(filename: str) -> str:
    name = Path(str(filename or "dictionary.dat")).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name[:180] or "dictionary.dat"


def _archive_dictionary_upload(settings: SubtitleServiceSettings, upload: UploadFile) -> Path:
    root = _dictionary_imports_dir(settings)
    archive_dir = root / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") / uuid.uuid4().hex[:12]
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / _safe_upload_filename(upload.filename or "dictionary.dat")
    with dest.open("wb") as out:
        shutil.copyfileobj(upload.file, out)
    return dest


def _is_missing_knowledge_table_error(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate == "42P01":  # PostgreSQL undefined_table
        return True
    msg = str(exc).lower()
    return (
        "does not exist" in msg
        and any(
            table in msg
            for table in [
                "translation_knowledge_items",
                "translation_term_evidence",
                "translation_term_matches",
                "translation_agent_runs",
                "translation_dictionary_sources",
                "translation_dictionary_import_batches",
                "translation_dictionary_entries",
            ]
        )
    )


def _ensure_rag_schema(settings: SubtitleServiceSettings) -> None:
    try:
        initialize_database(settings.database_url, force=True)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "RAG knowledge tables are not ready. Ensure PostgreSQL has pgvector installed "
                f"and restart the app. Migration error: {e}"
            ),
        ) from e


def _handle_knowledge_db_error(exc: Exception, settings: SubtitleServiceSettings) -> None:
    if _is_missing_knowledge_table_error(exc):
        _ensure_rag_schema(settings)
        return
    if isinstance(exc, ProgrammingError):
        raise HTTPException(status_code=503, detail=f"RAG database schema is not ready: {exc}") from exc
    raise HTTPException(status_code=500, detail=f"knowledge database error: {exc}") from exc


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class UploadTooLargeError(ValueError):
    pass


def _validate_model_name(name: str) -> str:
    name = (name or "").strip()
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid model name (allowed: [A-Za-z0-9._-], max 64 chars)")
    return name


def _dir_size_bytes(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            total += p.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _is_safe_zip_entry_name(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute():
        return False
    return all(part not in {"", ".", ".."} for part in path.parts)


def _safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    max_files: int,
    max_uncompressed_bytes: int,
) -> None:
    max_files = max(1, int(max_files or 1))
    max_uncompressed_bytes = max(1, int(max_uncompressed_bytes or 1))
    tmp_dir = dest_dir.parent / f".{dest_dir.name}.extract-{uuid.uuid4().hex}"
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise HTTPException(status_code=413, detail=f"zip contains too many entries (max {max_files})")
        total_uncompressed = 0
        for info in infos:
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if not _is_safe_zip_entry_name(name):
                raise HTTPException(status_code=400, detail=f"unsafe zip entry: {name}")
            total_uncompressed += int(info.file_size or 0)
            if total_uncompressed > max_uncompressed_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"zip uncompressed size too large (max {max_uncompressed_bytes} bytes)",
                )
        try:
            tmp_dir.mkdir(parents=True, exist_ok=False)
            zf.extractall(tmp_dir)
            tmp_dir.rename(dest_dir)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _startup()
    yield


app = FastAPI(title="videoroll-subtitle-service", version="0.1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
        if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_internal_service_auth(app, get_subtitle_settings)


def _startup() -> None:
    settings = get_subtitle_settings()
    app.state.internal_service_token = service_token(settings)
    initialize_database(settings.database_url)
    FileStore(settings).ensure_ready()
    _models_dir(settings).mkdir(parents=True, exist_ok=True)
    _dictionary_imports_dir(settings).mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/subtitle/settings", response_model=WhisperSettingsRead)
def get_subtitle_settings_view(settings: SubtitleServiceSettings = Depends(get_settings)) -> WhisperSettingsRead:
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        fw_installed = True
    except Exception:
        fw_installed = False
    try:
        import openvino_genai  # type: ignore  # noqa: F401

        ov_installed = True
    except Exception:
        ov_installed = False
    cpu_threads = int(getattr(settings, "whisper_cpu_threads", 0) or 0)
    num_workers = int(getattr(settings, "whisper_num_workers", 1) or 1)
    effective_threads = cpu_threads
    if effective_threads <= 0:
        effective_threads = process_cpu_count() or 4
    effective_workers = num_workers if num_workers > 0 else 1
    return WhisperSettingsRead(
        asr_engine=settings.asr_engine,
        whisper_model=settings.whisper_model,
        whisper_model_dir=settings.whisper_model_dir,
        whisper_device=settings.whisper_device,
        whisper_compute_type=settings.whisper_compute_type,
        openvino_model=settings.openvino_model,
        openvino_device=settings.openvino_device,
        openvino_num_beams=int(settings.openvino_num_beams or 1),
        openvino_max_new_tokens=int(settings.openvino_max_new_tokens or 448),
        openvino_vad_enabled=bool(settings.openvino_vad_enabled),
        openvino_vad_threshold=float(settings.openvino_vad_threshold or 0.5),
        external_whisper_base_url=str(settings.external_whisper_base_url or ""),
        external_whisper_model=str(settings.external_whisper_model or ""),
        external_whisper_api_key_set=bool(settings.external_whisper_api_key),
        external_whisper_batch_size=int(settings.external_whisper_batch_size or 1),
        external_whisper_vad_enabled=bool(settings.external_whisper_vad_enabled),
        external_whisper_vad_threshold=float(settings.external_whisper_vad_threshold or 0.5),
        external_whisper_min_silence_ms=int(settings.external_whisper_min_silence_ms or 500),
        external_whisper_speech_pad_ms=int(settings.external_whisper_speech_pad_ms or 180),
        external_whisper_condition_on_previous_text=bool(settings.external_whisper_condition_on_previous_text),
        external_whisper_max_segment_seconds=float(settings.external_whisper_max_segment_seconds or 6.0),
        external_whisper_max_segment_chars=int(settings.external_whisper_max_segment_chars or 80),
        groq_whisper_model=str(settings.groq_whisper_model or "whisper-large-v3-turbo"),
        groq_whisper_api_key_set=bool(settings.groq_whisper_api_key),
        cloudflare_workers_ai_account_id=str(settings.cloudflare_workers_ai_account_id or ""),
        cloudflare_workers_ai_model=str(settings.cloudflare_workers_ai_model or ""),
        cloudflare_workers_ai_api_key_set=bool(settings.cloudflare_workers_ai_api_key),
        whisper_cpu_threads=cpu_threads,
        whisper_num_workers=num_workers,
        whisper_cpu_threads_effective=int(effective_threads),
        whisper_num_workers_effective=int(effective_workers),
        faster_whisper_installed=fw_installed,
        openvino_installed=ov_installed,
    )


@app.get("/subtitle/hardware/intel", response_model=IntelHardwareProbeRead)
def get_intel_hardware_view(settings: SubtitleServiceSettings = Depends(get_settings)) -> IntelHardwareProbeRead:
    try:
        info = detect_intel_hardware(settings.intel_gpu_render_device)
    except Exception as e:
        info = {
            "checked": True,
            "available": False,
            "render_device": str(settings.intel_gpu_render_device or "").strip() or "/dev/dri/renderD128",
            "model_name": None,
            "driver": None,
            "pci_slot": None,
            "pci_id": None,
            "detail": str(e),
        }
    openvino_devices: list[str] = []
    openvino_error = ""
    try:
        import openvino as ov  # type: ignore

        openvino_devices = [str(device) for device in ov.Core().available_devices]
    except Exception as exc:
        openvino_error = f"{type(exc).__name__}: {exc}"
    info["openvino_devices"] = openvino_devices
    info["openvino_gpu_available"] = any(device.upper().startswith("GPU") for device in openvino_devices)
    info["openvino_error"] = openvino_error
    return IntelHardwareProbeRead(**info)


@app.get("/subtitle/asr/settings", response_model=ASRDefaultsRead)
def get_asr_settings_view(
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ASRDefaultsRead:
    cfg = get_asr_settings(db, settings)
    return ASRDefaultsRead(**cfg)


@app.put("/subtitle/asr/settings", response_model=ASRDefaultsRead)
def put_asr_settings_view(
    payload: ASRDefaultsUpdate,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ASRDefaultsRead:
    try:
        cfg = update_asr_settings(db, settings, payload.model_dump(exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ASRDefaultsRead(**cfg)


@app.post("/subtitle/asr/external/test", response_model=ExternalWhisperTestResponse)
def test_external_whisper(
    payload: ExternalWhisperTestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ExternalWhisperTestResponse:
    """Send a short generated WAV to an online/OpenAI-compatible Whisper API."""
    started = time.perf_counter()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="external-whisper-test-", suffix=".wav", delete=False) as handle:
            temp_path = Path(handle.name)
        with wave.open(str(temp_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 16000)
        stored = get_asr_settings(db, settings)
        api_key = str(payload.api_key or "").strip() or str(stored.get("external_whisper_api_key") or "").strip()
        segments = transcribe_external_whisper(
            temp_path,
            base_url=payload.base_url,
            api_key=api_key,
            model_name=payload.model,
            timeout_seconds=30.0,
            batch_size=int(payload.batch_size or stored.get("external_whisper_batch_size") or 1),
            vad_filter=(
                bool(payload.vad_enabled)
                if payload.vad_enabled is not None
                else bool(stored.get("external_whisper_vad_enabled", True))
            ),
            vad_threshold=float(payload.vad_threshold or stored.get("external_whisper_vad_threshold") or 0.5),
            min_silence_duration_ms=int(payload.min_silence_ms or stored.get("external_whisper_min_silence_ms") or 500),
            speech_pad_ms=int(
                payload.speech_pad_ms
                if payload.speech_pad_ms is not None
                else stored.get("external_whisper_speech_pad_ms") or 180
            ),
            condition_on_previous_text=(
                bool(payload.condition_on_previous_text)
                if payload.condition_on_previous_text is not None
                else bool(stored.get("external_whisper_condition_on_previous_text", False))
            ),
            max_segment_seconds=float(payload.max_segment_seconds or stored.get("external_whisper_max_segment_seconds") or 6.0),
            max_segment_chars=int(payload.max_segment_chars or stored.get("external_whisper_max_segment_chars") or 80),
        )
        return ExternalWhisperTestResponse(
            ok=True,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            text=" ".join(segment.text for segment in segments),
            segments=len(segments),
            longest_segment_seconds=max((segment.end - segment.start for segment in segments), default=0.0),
            longest_segment_chars=max((len(segment.text) for segment in segments), default=0),
        )
    except Exception as exc:
        return ExternalWhisperTestResponse(
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("failed to remove online Whisper test audio", exc_info=True)


@app.post("/subtitle/asr/cloudflare/test", response_model=CloudflareWorkersAITestResponse)
def test_cloudflare_workers_ai(
    payload: CloudflareWorkersAITestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> CloudflareWorkersAITestResponse:
    """Send a short generated WAV to Cloudflare Workers AI to verify settings."""
    started = time.perf_counter()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cloudflare-whisper-test-", suffix=".wav", delete=False) as handle:
            temp_path = Path(handle.name)
        with wave.open(str(temp_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 16000)
        stored = get_asr_settings(db, settings)
        api_key = str(payload.api_key or "").strip() or str(
            stored.get("cloudflare_workers_ai_api_key") or ""
        ).strip()
        segments = transcribe_cloudflare_workers_ai(
            temp_path,
            account_id=payload.account_id,
            api_key=api_key,
            model_name=payload.model,
            timeout_seconds=30.0,
        )
        return CloudflareWorkersAITestResponse(
            ok=True,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            text=" ".join(segment.text for segment in segments),
            segments=len(segments),
        )
    except Exception as exc:
        return CloudflareWorkersAITestResponse(
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("failed to remove Cloudflare Workers AI test audio", exc_info=True)


@app.post("/subtitle/asr/groq/test", response_model=GroqWhisperTestResponse)
def test_groq_whisper(
    payload: GroqWhisperTestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> GroqWhisperTestResponse:
    """Send a short WAV to Groq Whisper to verify the API key and model."""
    started = time.perf_counter()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="groq-whisper-test-", suffix=".wav", delete=False) as handle:
            temp_path = Path(handle.name)
        with wave.open(str(temp_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 16000)
        stored = get_asr_settings(db, settings)
        api_key = str(payload.api_key or "").strip() or str(stored.get("groq_whisper_api_key") or "").strip()
        segments = transcribe_groq_whisper(
            temp_path,
            api_key=api_key,
            model_name=payload.model,
            timeout_seconds=30.0,
            # This endpoint validates Groq connectivity with a generated
            # silent sample, so bypass local VAD to ensure a real API call.
            vad_enabled=False,
        )
        return GroqWhisperTestResponse(
            ok=True,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            text=" ".join(segment.text for segment in segments),
            segments=len(segments),
        )
    except Exception as exc:
        return GroqWhisperTestResponse(
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("failed to remove Groq Whisper test audio", exc_info=True)


@app.get("/subtitle/auto/profile", response_model=SubtitleAutoProfileRead)
def get_subtitle_auto_profile(db: Session = Depends(get_db)) -> SubtitleAutoProfileRead:
    cfg = get_auto_profile(db)
    return SubtitleAutoProfileRead(**cfg)


@app.put("/subtitle/auto/profile", response_model=SubtitleAutoProfileRead)
def put_subtitle_auto_profile(payload: SubtitleAutoProfileUpdate, db: Session = Depends(get_db)) -> SubtitleAutoProfileRead:
    try:
        cfg = update_auto_profile(db, payload.model_dump(exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SubtitleAutoProfileRead(**cfg)


@app.get("/subtitle/translate/settings", response_model=TranslateSettingsRead)
def get_translate_settings_view(
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TranslateSettingsRead:
    cfg = get_translate_settings(db, settings)
    return TranslateSettingsRead(
        default_provider=cfg["default_provider"],
        default_target_lang=cfg["default_target_lang"],
        default_style=cfg["default_style"],
        default_batch_size=cfg["default_batch_size"],
        default_max_retries=cfg["default_max_retries"],
        default_enable_summary=cfg["default_enable_summary"],
        openai_api_key_set=cfg["openai_api_key_set"],
        openai_base_url=cfg["openai_base_url"],
        openai_model=cfg["openai_model"],
        openai_temperature=cfg["openai_temperature"],
        openai_timeout_seconds=cfg["openai_timeout_seconds"],
        openai_max_retries=cfg["openai_max_retries"],
        openai_api_type=cfg["openai_api_type"],
        openai_enable_thinking=cfg["openai_enable_thinking"],
        cerebras_reasoning_effort=cfg["cerebras_reasoning_effort"],
        cerebras_reasoning_format=cfg["cerebras_reasoning_format"],
        rag_enabled=cfg["rag_enabled"],
        rag_top_k=cfg["rag_top_k"],
        rag_min_score=cfg["rag_min_score"],
        rag_embedding_provider=cfg["rag_embedding_provider"],
        rag_embedding_model=cfg["rag_embedding_model"],
        rag_embedding_dimensions=cfg["rag_embedding_dimensions"],
        rag_embedding_model_dir=cfg["rag_embedding_model_dir"],
        rag_embedding_device=cfg["rag_embedding_device"],
        rag_embedding_api_key_set=cfg["rag_embedding_api_key_set"],
        rag_embedding_base_url=cfg["rag_embedding_base_url"],
        rag_embedding_timeout_seconds=cfg["rag_embedding_timeout_seconds"],
        rag_auto_discover_terms=cfg["rag_auto_discover_terms"],
        rag_auto_learn_terms=cfg["rag_auto_learn_terms"],
        rag_dictionary_enabled=cfg["rag_dictionary_enabled"],
        rag_dictionary_top_k=cfg["rag_dictionary_top_k"],
        rag_dictionary_min_quality=cfg["rag_dictionary_min_quality"],
        rag_dictionary_auto_promote=cfg["rag_dictionary_auto_promote"],
        rag_wiki_enabled=cfg["rag_wiki_enabled"],
        rag_search_enabled=cfg["rag_search_enabled"],
        rag_search_url=cfg["rag_search_url"],
        rag_search_categories=cfg["rag_search_categories"],
        rag_search_engines=cfg["rag_search_engines"],
        rag_search_fallback_engines=cfg["rag_search_fallback_engines"],
        rag_search_language=cfg["rag_search_language"],
        rag_search_safesearch=cfg["rag_search_safesearch"],
        rag_search_time_range=cfg["rag_search_time_range"],
        rag_search_pageno=cfg["rag_search_pageno"],
        rag_domain=cfg["rag_domain"],
        rag_agent_parallelism=cfg["rag_agent_parallelism"],
        rag_agent_timeout_seconds=cfg["rag_agent_timeout_seconds"],
        rag_agent_skills_enabled=cfg["rag_agent_skills_enabled"],
        rag_agent_builtin_skills_enabled=cfg["rag_agent_builtin_skills_enabled"],
        rag_agent_user_skills_enabled=cfg["rag_agent_user_skills_enabled"],
    )


@app.put("/subtitle/translate/settings", response_model=TranslateSettingsRead)
def put_translate_settings_view(
    payload: TranslateSettingsUpdate,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TranslateSettingsRead:
    cfg = update_translate_settings(db, settings, payload.model_dump(exclude_unset=True))
    return TranslateSettingsRead(
        default_provider=cfg["default_provider"],
        default_target_lang=cfg["default_target_lang"],
        default_style=cfg["default_style"],
        default_batch_size=cfg["default_batch_size"],
        default_max_retries=cfg["default_max_retries"],
        default_enable_summary=cfg["default_enable_summary"],
        openai_api_key_set=cfg["openai_api_key_set"],
        openai_base_url=cfg["openai_base_url"],
        openai_model=cfg["openai_model"],
        openai_temperature=cfg["openai_temperature"],
        openai_timeout_seconds=cfg["openai_timeout_seconds"],
        openai_max_retries=cfg["openai_max_retries"],
        openai_api_type=cfg["openai_api_type"],
        openai_enable_thinking=cfg["openai_enable_thinking"],
        cerebras_reasoning_effort=cfg["cerebras_reasoning_effort"],
        cerebras_reasoning_format=cfg["cerebras_reasoning_format"],
        rag_enabled=cfg["rag_enabled"],
        rag_top_k=cfg["rag_top_k"],
        rag_min_score=cfg["rag_min_score"],
        rag_embedding_provider=cfg["rag_embedding_provider"],
        rag_embedding_model=cfg["rag_embedding_model"],
        rag_embedding_dimensions=cfg["rag_embedding_dimensions"],
        rag_embedding_model_dir=cfg["rag_embedding_model_dir"],
        rag_embedding_device=cfg["rag_embedding_device"],
        rag_embedding_api_key_set=cfg["rag_embedding_api_key_set"],
        rag_embedding_base_url=cfg["rag_embedding_base_url"],
        rag_embedding_timeout_seconds=cfg["rag_embedding_timeout_seconds"],
        rag_auto_discover_terms=cfg["rag_auto_discover_terms"],
        rag_auto_learn_terms=cfg["rag_auto_learn_terms"],
        rag_dictionary_enabled=cfg["rag_dictionary_enabled"],
        rag_dictionary_top_k=cfg["rag_dictionary_top_k"],
        rag_dictionary_min_quality=cfg["rag_dictionary_min_quality"],
        rag_dictionary_auto_promote=cfg["rag_dictionary_auto_promote"],
        rag_wiki_enabled=cfg["rag_wiki_enabled"],
        rag_search_enabled=cfg["rag_search_enabled"],
        rag_search_url=cfg["rag_search_url"],
        rag_search_categories=cfg["rag_search_categories"],
        rag_search_engines=cfg["rag_search_engines"],
        rag_search_fallback_engines=cfg["rag_search_fallback_engines"],
        rag_search_language=cfg["rag_search_language"],
        rag_search_safesearch=cfg["rag_search_safesearch"],
        rag_search_time_range=cfg["rag_search_time_range"],
        rag_search_pageno=cfg["rag_search_pageno"],
        rag_domain=cfg["rag_domain"],
        rag_agent_parallelism=cfg["rag_agent_parallelism"],
        rag_agent_timeout_seconds=cfg["rag_agent_timeout_seconds"],
        rag_agent_skills_enabled=cfg["rag_agent_skills_enabled"],
        rag_agent_builtin_skills_enabled=cfg["rag_agent_builtin_skills_enabled"],
        rag_agent_user_skills_enabled=cfg["rag_agent_user_skills_enabled"],
    )


@app.get("/subtitle/knowledge/items", response_model=list[KnowledgeItemRead])
def list_knowledge_items_view(
    item_type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    domain: str | None = None,
    limit: int = 100,
    offset: int = 0,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[KnowledgeItemRead]:
    try:
        rows = list_knowledge_items(db, item_type=item_type, status=status, q=q, domain=domain, limit=limit, offset=offset)
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            _handle_knowledge_db_error(e, settings)
        _ensure_rag_schema(settings)
        try:
            rows = list_knowledge_items(db, item_type=item_type, status=status, q=q, domain=domain, limit=limit, offset=offset)
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"RAG knowledge tables are not ready: {retry_error}") from retry_error
    return [KnowledgeItemRead(**row) for row in rows]


@app.post("/subtitle/knowledge/items", response_model=KnowledgeItemUpsertResponse)
def upsert_knowledge_item_view(
    payload: KnowledgeItemUpsertRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> KnowledgeItemUpsertResponse:
    cfg = get_translate_settings(db, settings)
    rag_cfg = rag_settings_from_translate_settings(cfg)

    if payload.item_type == "term" and not payload.term.strip():
        raise HTTPException(status_code=400, detail="term is required for term items")
    if payload.item_type == "term" and not payload.translation.strip():
        raise HTTPException(status_code=400, detail="translation is required for term items")
    if payload.item_type == "document" and not (payload.title.strip() or payload.content.strip()):
        raise HTTPException(status_code=400, detail="title or content is required for document items")

    embedding_text = build_knowledge_embedding_text(
        item_type=payload.item_type,
        term=payload.term,
        translation=payload.translation,
        domain=payload.domain,
        aliases=payload.aliases,
        title=payload.title,
        content=payload.content,
        description=payload.description,
    )

    embedding: list[float] | None = None
    if embedding_text.strip():
        try:
            embedding = embed_text(embedding_text, settings=embedding_settings_from_translate_settings(cfg))
            assert_embedding_dimensions(embedding, rag_cfg.embedding_dimensions)
        except Exception as e:
            if payload.item_type == "document":
                raise HTTPException(status_code=502, detail=f"embedding failed: {e}") from e
            embedding = None

    def _save_item() -> str:
        item_id = upsert_knowledge_item(
            db,
            item_type=payload.item_type,
            target_lang=payload.target_lang,
            term=payload.term,
            translation=payload.translation,
            domain=payload.domain,
            aliases=payload.aliases,
            title=payload.title,
            content=payload.content,
            description=payload.description,
            sources=payload.sources,
            confidence=payload.confidence,
            status=payload.status,
            created_by=payload.created_by,
            embedding=embedding,
            embedding_model=f"{rag_cfg.embedding_provider}:{rag_cfg.embedding_model}" if embedding else "",
        )
        db.commit()
        return item_id

    try:
        item_id = _save_item()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"knowledge item save failed: {e}") from e
        _ensure_rag_schema(settings)
        try:
            item_id = _save_item()
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"knowledge item save failed after schema migration: {retry_error}") from retry_error

    return KnowledgeItemUpsertResponse(id=uuid.UUID(item_id))


@app.delete("/subtitle/knowledge/items/{item_id}")
def delete_knowledge_item_view(
    item_id: uuid.UUID,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    try:
        deleted = delete_knowledge_item(db, str(item_id))
        db.commit()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"knowledge item delete failed: {e}") from e
        _ensure_rag_schema(settings)
        try:
            deleted = delete_knowledge_item(db, str(item_id))
            db.commit()
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"knowledge item delete failed after schema migration: {retry_error}") from retry_error
    if not deleted:
        raise HTTPException(status_code=404, detail="knowledge item not found")
    return {"deleted": True}


@app.post("/subtitle/knowledge/rebuild-embeddings", response_model=KnowledgeEmbeddingRebuildResponse)
def rebuild_knowledge_embeddings_view(
    payload: KnowledgeEmbeddingRebuildRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> KnowledgeEmbeddingRebuildResponse:
    cfg = get_translate_settings(db, settings)
    rag_cfg = rag_settings_from_translate_settings(cfg)
    emb_cfg = embedding_settings_from_translate_settings(cfg)
    def _rebuild() -> dict[str, Any]:
        result = rebuild_knowledge_embeddings(
            db,
            rag_settings=rag_cfg,
            embedding_settings=emb_cfg,
            item_type=payload.item_type,
            status=payload.status,
            limit=payload.limit,
        )
        db.commit()
        return result

    try:
        result = _rebuild()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=502, detail=f"knowledge embedding rebuild failed: {e}") from e
        _ensure_rag_schema(settings)
        try:
            result = _rebuild()
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"knowledge embedding rebuild failed after schema migration: {retry_error}") from retry_error
    return KnowledgeEmbeddingRebuildResponse(**result)


def _parse_knowledge_import(
    *,
    filename: str,
    raw: bytes,
    import_format: str,
    target_lang: str,
    domain: str,
) -> list[dict[str, Any]]:
    fmt = str(import_format or "").strip().lower()
    if fmt in {"", "auto"}:
        suffix = Path(filename or "").suffix.lower().lstrip(".")
        fmt = "json" if suffix == "json" else "csv" if suffix == "csv" else "srt" if suffix == "srt" else ""
    if fmt not in {"csv", "json", "srt"}:
        raise HTTPException(status_code=400, detail="knowledge import format must be csv, json, or srt")

    try:
        text_value = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="knowledge import file must be UTF-8") from exc

    if fmt == "json":
        try:
            payload = json.loads(text_value)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        rows = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail="JSON import must be an array or an object with an items array")
        return [dict(row) for row in rows if isinstance(row, dict)]

    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(text_value))
        if not reader.fieldnames:
            raise HTTPException(status_code=400, detail="CSV import has no header")
        rows: list[dict[str, Any]] = []
        for row in reader:
            item = {str(key or "").strip(): str(value or "").strip() for key, value in row.items()}
            aliases = [part.strip() for part in re.split(r"[|;]", item.get("aliases", "")) if part.strip()]
            item["aliases"] = aliases
            rows.append(item)
        return rows

    # SRT is imported as chunked document knowledge. Keeping chunks bounded
    # avoids a single hour-long subtitle file producing an oversized embedding
    # request while preserving useful neighboring subtitle context.
    cues: list[str] = []
    for block in re.split(r"\r?\n\s*\r?\n", text_value):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines and lines[0].isdigit():
            lines = lines[1:]
        if lines and "-->" in lines[0]:
            lines = lines[1:]
        cue = " ".join(lines).strip()
        if cue:
            cues.append(cue)

    rows = []
    chunk: list[str] = []
    chunk_len = 0
    part = 1
    for cue in cues:
        if chunk and (len(chunk) >= 50 or chunk_len + len(cue) + 1 > 4000):
            rows.append(
                {
                    "item_type": "document",
                    "target_lang": target_lang,
                    "domain": domain,
                    "title": f"{Path(filename or 'subtitle.srt').name} · part {part}",
                    "content": "\n".join(chunk),
                    "created_by": "bulk:srt",
                }
            )
            part += 1
            chunk = []
            chunk_len = 0
        chunk.append(cue)
        chunk_len += len(cue) + 1
    if chunk:
        rows.append(
            {
                "item_type": "document",
                "target_lang": target_lang,
                "domain": domain,
                "title": f"{Path(filename or 'subtitle.srt').name} · part {part}",
                "content": "\n".join(chunk),
                "created_by": "bulk:srt",
            }
        )
    return rows


@app.post("/subtitle/knowledge/import", response_model=KnowledgeBulkImportResponse)
async def import_knowledge_items_view(
    file: UploadFile = File(...),
    import_format: str = Form("auto"),
    target_lang: str = Form("zh"),
    domain: str = Form(""),
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> KnowledgeBulkImportResponse:
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="knowledge import file exceeds 5 MiB")

    rows = _parse_knowledge_import(
        filename=file.filename or "knowledge",
        raw=raw,
        import_format=import_format,
        target_lang=target_lang,
        domain=domain,
    )
    if len(rows) > 2000:
        raise HTTPException(status_code=400, detail="knowledge import contains more than 2000 items")

    cfg = get_translate_settings(db, settings)
    rag_cfg = rag_settings_from_translate_settings(cfg)
    emb_cfg = embedding_settings_from_translate_settings(cfg)
    imported_ids: list[uuid.UUID] = []
    errors: list[dict[str, str]] = []
    skipped = 0
    failed = 0

    for index, raw_item in enumerate(rows):
        data = dict(raw_item)
        data.setdefault("target_lang", target_lang)
        data.setdefault("domain", domain)
        data.setdefault("created_by", "bulk:import")
        if isinstance(data.get("aliases"), str):
            data["aliases"] = [part.strip() for part in re.split(r"[|;]", str(data["aliases"])) if part.strip()]
        try:
            item = KnowledgeItemUpsertRequest.model_validate(data)
            if item.item_type == "term" and (not item.term.strip() or not item.translation.strip()):
                skipped += 1
                continue
            if item.item_type == "document" and not (item.title.strip() or item.content.strip()):
                skipped += 1
                continue

            embedding_text = build_knowledge_embedding_text(
                item_type=item.item_type,
                term=item.term,
                translation=item.translation,
                domain=item.domain,
                aliases=item.aliases,
                title=item.title,
                content=item.content,
                description=item.description,
            )
            embedding: list[float] | None = None
            if embedding_text.strip():
                try:
                    embedding = embed_text(embedding_text, settings=emb_cfg)
                    assert_embedding_dimensions(embedding, rag_cfg.embedding_dimensions)
                except Exception:
                    # Term rows remain useful for exact dictionary-style
                    # matching even when the embedding provider is temporarily
                    # unavailable. Documents require vectors to be useful.
                    if item.item_type == "document":
                        raise

            item_id = upsert_knowledge_item(
                db,
                item_type=item.item_type,
                target_lang=item.target_lang,
                term=item.term,
                translation=item.translation,
                domain=item.domain,
                aliases=item.aliases,
                title=item.title,
                content=item.content,
                description=item.description,
                sources=item.sources,
                confidence=item.confidence,
                status=item.status,
                created_by=item.created_by,
                embedding=embedding,
                embedding_model=f"{rag_cfg.embedding_provider}:{rag_cfg.embedding_model}" if embedding else "",
                dedupe_any_domain=False,
            )
            db.commit()
            imported_ids.append(uuid.UUID(item_id))
        except Exception as exc:
            db.rollback()
            failed += 1
            if len(errors) < 50:
                errors.append({"row": str(index + 1), "error": f"{type(exc).__name__}: {exc}"[:1000]})

    suffix = Path(file.filename or "").suffix.lower().lstrip(".")
    resolved_format = import_format.strip().lower()
    if resolved_format in {"", "auto"}:
        resolved_format = suffix
    return KnowledgeBulkImportResponse(
        format=resolved_format,
        parsed=len(rows),
        imported=len(imported_ids),
        failed=failed,
        skipped=skipped,
        ids=imported_ids,
        errors=errors,
    )


@app.get("/subtitle/dictionaries/sources", response_model=list[DictionarySourceRead])
def list_dictionary_sources_view(
    enabled: bool | None = None,
    q: str | None = None,
    limit: int = 100,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[DictionarySourceRead]:
    try:
        rows = list_dictionary_sources(db, enabled=enabled, q=q, limit=limit)
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary source list failed: {e}") from e
        _ensure_rag_schema(settings)
        rows = list_dictionary_sources(db, enabled=enabled, q=q, limit=limit)
    return [DictionarySourceRead(**row) for row in rows]


@app.put("/subtitle/dictionaries/sources/{source_id}", response_model=DictionarySourceRead)
def update_dictionary_source_view(
    source_id: uuid.UUID,
    payload: DictionarySourceUpdate,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> DictionarySourceRead:
    try:
        row = update_dictionary_source(db, str(source_id), **payload.model_dump(exclude_unset=True))
        db.commit()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary source update failed: {e}") from e
        _ensure_rag_schema(settings)
        row = update_dictionary_source(db, str(source_id), **payload.model_dump(exclude_unset=True))
        db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="dictionary source not found")
    return DictionarySourceRead(**row)


@app.delete("/subtitle/dictionaries/sources/{source_id}")
def delete_dictionary_source_view(
    source_id: uuid.UUID,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    try:
        deleted = delete_dictionary_source(db, str(source_id))
        db.commit()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary source delete failed: {e}") from e
        _ensure_rag_schema(settings)
        deleted = delete_dictionary_source(db, str(source_id))
        db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="dictionary source not found")
    return {"deleted": True}


@app.get("/subtitle/dictionaries/import-presets")
def list_dictionary_import_presets_view() -> list[dict[str, Any]]:
    return dictionary_import_presets()


@app.post("/subtitle/dictionaries/import", response_model=DictionaryImportResponse)
def import_dictionary_view(
    file: UploadFile = File(...),
    dictionary_preset: str = Form(""),
    name: str = Form(""),
    slug: str = Form(""),
    description: str = Form(""),
    source_lang: str = Form(""),
    target_lang: str = Form("zh"),
    format_name: str = Form("auto"),
    license: str = Form(""),
    license_url: str = Form(""),
    source_url: str = Form(""),
    version: str = Form(""),
    attribution: str = Form(""),
    domain: str = Form(""),
    priority: int = Form(0),
    enabled: bool = Form(True),
    import_mode: str = Form("upsert"),
    full_import: bool = Form(False),
    max_entries: int = Form(250000),
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> DictionaryImportResponse:
    archive_path = _archive_dictionary_upload(settings, file)
    preset = get_dictionary_import_preset(dictionary_preset)
    if dictionary_preset.strip() and preset is None:
        raise HTTPException(status_code=400, detail=f"unsupported dictionary_preset: {dictionary_preset}")
    if preset:
        format_name = str(preset.get("format_name") or format_name or "auto")
        source_lang = source_lang.strip() or str(preset.get("source_lang") or "")
        target_lang = target_lang.strip() or str(preset.get("target_lang") or "zh")
        slug = slug.strip() or str(preset.get("slug") or "")
        description = description.strip() or str(preset.get("description") or "")
        license = license.strip() or str(preset.get("license") or "")
        license_url = license_url.strip() or str(preset.get("license_url") or "")
        source_url = source_url.strip() or str(preset.get("source_url") or "")
        version = version.strip() or str(preset.get("version") or "")
        domain = domain.strip() or str(preset.get("domain") or "")
        if priority == 0:
            priority = int(preset.get("priority") or 0)
        if bool(preset.get("recommended_full_import")) and max_entries == 250000:
            full_import = True
    source_name = name.strip() or str((preset or {}).get("name") or "").strip() or Path(file.filename or archive_path.name).stem or "Dictionary"
    clean_mode = import_mode.strip().lower() or "upsert"
    if clean_mode not in {"upsert", "replace"}:
        raise HTTPException(status_code=400, detail="import_mode must be upsert or replace")
    clean_max_entries = 0 if full_import else max(1, min(1_000_000, int(max_entries or 250000)))

    def _run_import() -> dict[str, Any]:
        result = import_dictionary_file(
            db,
            path=archive_path,
            filename=file.filename or archive_path.name,
            archive_path=str(archive_path),
            source_name=source_name,
            slug=slug,
            description=description,
            source_lang=source_lang,
            target_lang=target_lang,
            format_name=format_name,
            license=license,
            license_url=license_url,
            source_url=source_url,
            version=version,
            attribution=attribution,
            domain=domain,
            priority=priority,
            enabled=enabled,
            import_mode=clean_mode,
            max_entries=clean_max_entries,
        )
        db.commit()
        return result

    try:
        result = _run_import()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary import failed: {e}") from e
        _ensure_rag_schema(settings)
        try:
            result = _run_import()
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"dictionary import failed after schema migration: {retry_error}") from retry_error
    return DictionaryImportResponse(**result)


@app.get("/subtitle/dictionaries/entries", response_model=list[DictionaryEntryRead])
def list_dictionary_entries_view(
    source_id: uuid.UUID | None = None,
    q: str | None = None,
    source_lang: str | None = None,
    target_lang: str | None = None,
    domain: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[DictionaryEntryRead]:
    try:
        rows = list_dictionary_entries(
            db,
            source_id=str(source_id) if source_id else None,
            q=q,
            source_lang=source_lang,
            target_lang=target_lang,
            domain=domain,
            enabled=enabled,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary entry list failed: {e}") from e
        _ensure_rag_schema(settings)
        rows = list_dictionary_entries(
            db,
            source_id=str(source_id) if source_id else None,
            q=q,
            source_lang=source_lang,
            target_lang=target_lang,
            domain=domain,
            enabled=enabled,
            limit=limit,
            offset=offset,
        )
    return [DictionaryEntryRead(**row) for row in rows]


@app.put("/subtitle/dictionaries/entries/{entry_id}", response_model=DictionaryEntryRead)
def update_dictionary_entry_view(
    entry_id: uuid.UUID,
    payload: DictionaryEntryUpdate,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> DictionaryEntryRead:
    try:
        row = set_dictionary_entry_enabled(db, str(entry_id), payload.enabled)
        db.commit()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary entry update failed: {e}") from e
        _ensure_rag_schema(settings)
        row = set_dictionary_entry_enabled(db, str(entry_id), payload.enabled)
        db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="dictionary entry not found")
    return DictionaryEntryRead(**row)


@app.delete("/subtitle/dictionaries/entries/{entry_id}")
def delete_dictionary_entry_view(
    entry_id: uuid.UUID,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    try:
        deleted = delete_dictionary_entry(db, str(entry_id))
        db.commit()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary entry delete failed: {e}") from e
        _ensure_rag_schema(settings)
        deleted = delete_dictionary_entry(db, str(entry_id))
        db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="dictionary entry not found")
    return {"deleted": True}


@app.post("/subtitle/dictionaries/lookup", response_model=DictionaryLookupResponse)
def lookup_dictionary_view(
    payload: DictionaryLookupRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> DictionaryLookupResponse:
    try:
        rows = lookup_dictionary_entries(
            db,
            term=payload.term,
            source_lang=payload.source_lang,
            target_lang=payload.target_lang,
            domain=payload.domain,
            limit=payload.limit,
            min_quality=payload.min_quality,
            exact=payload.exact,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary lookup failed: {e}") from e
        _ensure_rag_schema(settings)
        rows = lookup_dictionary_entries(
            db,
            term=payload.term,
            source_lang=payload.source_lang,
            target_lang=payload.target_lang,
            domain=payload.domain,
            limit=payload.limit,
            min_quality=payload.min_quality,
            exact=payload.exact,
        )
        db.commit()
    return DictionaryLookupResponse(count=len(rows), results=[DictionaryEntryRead(**row) for row in rows])


@app.post("/subtitle/dictionaries/promote", response_model=DictionaryPromoteResponse)
def promote_dictionary_entry_view(
    payload: DictionaryPromoteRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> DictionaryPromoteResponse:
    try:
        entry = get_dictionary_entry(db, str(payload.entry_id))
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary entry read failed: {e}") from e
        _ensure_rag_schema(settings)
        entry = get_dictionary_entry(db, str(payload.entry_id))
    if not entry:
        raise HTTPException(status_code=404, detail="dictionary entry not found")
    translations = [str(x) for x in entry.get("translations") or [] if str(x or "").strip()]
    if not translations:
        raise HTTPException(status_code=400, detail="dictionary entry has no translation")
    cfg = get_translate_settings(db, settings)
    rag_cfg = rag_settings_from_translate_settings(cfg)
    sources = [
        {
            "source": entry.get("source_name") or entry.get("source_slug") or "dictionary",
            "url": entry.get("source_url") or entry.get("license_url") or "",
            "license": entry.get("license") or "",
            "attribution": entry.get("attribution") or "",
            "dictionary_entry_id": str(payload.entry_id),
        }
    ]
    aliases = [str(x) for x in entry.get("aliases") or [] if str(x or "").strip()]
    embedding_text = build_knowledge_embedding_text(
        item_type="term",
        term=str(entry.get("term") or ""),
        translation=translations[0],
        domain=str(entry.get("domain") or rag_cfg.domain or ""),
        aliases=aliases,
        description=str(entry.get("definition") or entry.get("pos") or ""),
    )
    embedding: list[float] | None = None
    if embedding_text.strip():
        try:
            embedding = embed_text(embedding_text, settings=embedding_settings_from_translate_settings(cfg))
            assert_embedding_dimensions(embedding, rag_cfg.embedding_dimensions)
        except Exception:
            embedding = None
    try:
        item_id = upsert_knowledge_item(
            db,
            item_type="term",
            target_lang=str(entry.get("target_lang") or "zh"),
            term=str(entry.get("term") or ""),
            translation=translations[0],
            domain=str(entry.get("domain") or rag_cfg.domain or ""),
            aliases=aliases,
            description=str(entry.get("definition") or entry.get("pos") or ""),
            sources=sources,
            confidence=max(float(entry.get("quality") or 0.0), float(payload.confidence or 0.0)),
            status=payload.status,
            created_by="dictionary_import",
            embedding=embedding,
            embedding_model=f"{rag_cfg.embedding_provider}:{rag_cfg.embedding_model}" if embedding else "",
            dedupe_any_domain=True,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            raise HTTPException(status_code=500, detail=f"dictionary promote failed: {e}") from e
        _ensure_rag_schema(settings)
        item_id = upsert_knowledge_item(
            db,
            item_type="term",
            target_lang=str(entry.get("target_lang") or "zh"),
            term=str(entry.get("term") or ""),
            translation=translations[0],
            domain=str(entry.get("domain") or rag_cfg.domain or ""),
            aliases=aliases,
            description=str(entry.get("definition") or entry.get("pos") or ""),
            sources=sources,
            confidence=max(float(entry.get("quality") or 0.0), float(payload.confidence or 0.0)),
            status=payload.status,
            created_by="dictionary_import",
            embedding=embedding,
            embedding_model=f"{rag_cfg.embedding_provider}:{rag_cfg.embedding_model}" if embedding else "",
            dedupe_any_domain=True,
        )
        db.commit()
    return DictionaryPromoteResponse(knowledge_item_id=uuid.UUID(item_id))


@app.get("/subtitle/agents/runs", response_model=list[AgentRunRead])
def list_agent_runs_view(
    status: str | None = None,
    limit: int = 30,
    include_descendants: bool = False,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[AgentRunRead]:
    try:
        rows = list_agent_runs(db, status=status, limit=limit, include_descendants=include_descendants)
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            _handle_knowledge_db_error(e, settings)
        _ensure_rag_schema(settings)
        try:
            rows = list_agent_runs(db, status=status, limit=limit, include_descendants=include_descendants)
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"RAG agent tables are not ready: {retry_error}") from retry_error
    return [AgentRunRead(**row) for row in rows]


@app.get("/subtitle/agents/runs/{run_id}", response_model=AgentRunRead)
def get_agent_run_view(
    run_id: uuid.UUID,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AgentRunRead:
    try:
        row = get_agent_run(db, str(run_id))
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            _handle_knowledge_db_error(e, settings)
        _ensure_rag_schema(settings)
        try:
            row = get_agent_run(db, str(run_id))
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"RAG agent tables are not ready: {retry_error}") from retry_error
    if row is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return AgentRunRead(**row)


@app.get("/subtitle/agent/skills", response_model=list[AgentSkillRead])
def list_agent_skills_view(
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[AgentSkillRead]:
    cfg = get_translate_settings(db, settings)
    registry = load_agent_skill_registry(rag_settings_from_translate_settings(cfg), force=True)
    return [AgentSkillRead(**item) for item in registry.summaries()]


@app.get("/subtitle/embedding/models", response_model=list[EmbeddingModelInfo])
def list_embedding_models(
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[EmbeddingModelInfo]:
    cfg = get_translate_settings(db, settings)
    return [EmbeddingModelInfo(**item) for item in list_local_embedding_models(_embedding_models_dir_from_translate_settings(cfg, settings))]


@app.post("/subtitle/embedding/models/list", response_model=list[EmbeddingModelInfo])
def list_embedding_models_for_dir(
    payload: EmbeddingModelListRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[EmbeddingModelInfo]:
    cfg = get_translate_settings(db, settings)
    if payload.model_dir is not None:
        cfg["rag_embedding_model_dir"] = payload.model_dir
    return [EmbeddingModelInfo(**item) for item in list_local_embedding_models(_embedding_models_dir_from_translate_settings(cfg, settings))]


@app.post("/subtitle/embedding/models/download", response_model=EmbeddingModelInfo)
def download_embedding_model(
    payload: EmbeddingModelDownloadRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> EmbeddingModelInfo:
    asr_cfg = get_asr_settings(db, settings)
    cfg = get_translate_settings(db, settings)
    if payload.model_dir is not None:
        cfg["rag_embedding_model_dir"] = payload.model_dir
    proxy = str(asr_cfg.get("model_download_proxy") or "").strip() or None
    try:
        dest = download_local_embedding_model(
            model=payload.model,
            model_dir=_embedding_models_dir_from_translate_settings(cfg, settings),
            name=payload.name,
            revision=payload.revision,
            force=payload.force,
            proxy=proxy,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    return EmbeddingModelInfo(name=dest.name, path=str(dest), size_bytes=size)


@app.delete("/subtitle/embedding/models/{name}")
def delete_embedding_model(
    name: str,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    model_name = _validate_model_name(name)
    cfg = get_translate_settings(db, settings)
    delete_local_embedding_model(model_dir=_embedding_models_dir_from_translate_settings(cfg, settings), name=model_name)
    return {"deleted": True}


@app.post("/subtitle/embedding/test", response_model=EmbeddingTestResponse)
def test_embedding(
    payload: EmbeddingTestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> EmbeddingTestResponse:
    text = str(payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text too long (max 2000 chars)")

    cfg = get_translate_settings(db, settings)
    if payload.provider is not None:
        cfg["rag_embedding_provider"] = payload.provider
    if payload.model is not None:
        cfg["rag_embedding_model"] = payload.model
    if payload.api_key is not None:
        cfg["rag_embedding_api_key"] = payload.api_key
    if payload.base_url is not None:
        cfg["rag_embedding_base_url"] = payload.base_url
    if payload.timeout_seconds is not None:
        cfg["rag_embedding_timeout_seconds"] = payload.timeout_seconds
    if payload.model_dir is not None:
        cfg["rag_embedding_model_dir"] = payload.model_dir
    if payload.dimensions is not None:
        cfg["rag_embedding_dimensions"] = payload.dimensions
    if payload.device is not None:
        cfg["rag_embedding_device"] = payload.device

    emb_settings = embedding_settings_from_translate_settings(cfg)
    try:
        vector = embed_text(text, settings=emb_settings)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    expected = int(cfg.get("rag_embedding_dimensions") or len(vector))
    return EmbeddingTestResponse(
        provider=emb_settings.provider,
        model=emb_settings.model,
        dimensions=len(vector),
        expected_dimensions=expected,
        ok=len(vector) == expected,
    )


@app.get("/subtitle/embedding/runtime", response_model=EmbeddingRuntimeStatusRead)
def get_embedding_runtime_status(
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> EmbeddingRuntimeStatusRead:
    cfg = get_translate_settings(db, settings)
    provider = str(cfg.get("rag_embedding_provider") or "").strip()
    model = str(cfg.get("rag_embedding_model") or "").strip()
    dimensions = max(1, int(cfg.get("rag_embedding_dimensions") or 1))
    device = str(cfg.get("rag_embedding_device") or "").strip()
    base = {
        "provider": provider,
        "model": model,
        "configured_dimensions": dimensions,
        "device": device,
    }
    try:
        bind = db.get_bind()
        if bind.dialect.name != "postgresql":
            return EmbeddingRuntimeStatusRead(
                **base,
                supported=False,
                search_mode="unavailable",
                detail=f"runtime vector diagnostics require PostgreSQL/pgvector (current dialect: {bind.dialect.name})",
            )

        pgvector_version = str(
            db.execute(text("SELECT COALESCE((SELECT extversion FROM pg_extension WHERE extname = 'vector'), '')")).scalar()
            or ""
        )
        column_type = str(
            db.execute(
                text(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod)
                    FROM pg_attribute a
                    JOIN pg_class t ON t.oid = a.attrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE n.nspname = current_schema()
                      AND t.relname = 'translation_knowledge_items'
                      AND a.attname = 'embedding'
                      AND NOT a.attisdropped
                    """
                )
            ).scalar()
            or ""
        )
        rows = db.execute(
            text(
                """
                SELECT COALESCE(embedding_model, '') AS embedding_model,
                       vector_dims(embedding) AS dimensions,
                       COUNT(*) AS count
                FROM translation_knowledge_items
                WHERE embedding IS NOT NULL
                GROUP BY 1, 2
                ORDER BY count DESC, embedding_model, dimensions
                """
            )
        ).all()
        buckets = [
            {"embedding_model": str(row[0] or ""), "dimensions": int(row[1]), "count": int(row[2])}
            for row in rows
        ]
        total_embeddings = sum(bucket["count"] for bucket in buckets)
        active_embeddings = sum(
            bucket["count"]
            for bucket in buckets
            if bucket["dimensions"] == dimensions and (not model or bucket["embedding_model"] == model or bucket["embedding_model"].endswith(f":{model}"))
        )
        hnsw_rows = db.execute(
            text(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'translation_knowledge_items'
                  AND lower(indexdef) LIKE '% using hnsw %'
                """
            )
        ).all()
        hnsw_index_present = bool(hnsw_rows)
        fixed_dimension_type = column_type.lower() == f"vector({dimensions})"
        hnsw_usable = hnsw_index_present and fixed_dimension_type
        detail = ""
        if not pgvector_version:
            detail = "pgvector extension is not installed"
        elif not hnsw_usable:
            if column_type.lower() == "vector":
                detail = "embedding column has variable dimensions; current raw-vector HNSW query is unavailable"
            elif hnsw_index_present:
                detail = "HNSW index exists but does not match the configured vector dimension/query shape"
            else:
                detail = "no usable HNSW index for the current embedding query"
        return EmbeddingRuntimeStatusRead(
            **base,
            supported=bool(pgvector_version),
            pgvector_version=pgvector_version,
            column_type=column_type,
            total_embeddings=total_embeddings,
            active_embeddings=active_embeddings,
            buckets=buckets,
            hnsw_index_present=hnsw_index_present,
            hnsw_usable_for_current_query=hnsw_usable,
            search_mode="hnsw" if hnsw_usable else ("exact_scan" if pgvector_version else "unavailable"),
            detail=detail,
        )
    except Exception as exc:
        logger.warning("embedding runtime diagnostics failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return EmbeddingRuntimeStatusRead(
            **base,
            supported=False,
            search_mode="unavailable",
            detail=f"{type(exc).__name__}: {exc}",
        )


@app.post("/subtitle/translate/test", response_model=TranslateTestResponse)
def translate_test(
    payload: TranslateTestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TranslateTestResponse:
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text too long (max 2000 chars)")

    try:
        ai_service = AIService(lambda: get_translate_settings(db, settings))
        translated = ai_service.translate_text(
            text,
            target_lang=payload.target_lang,
            style=payload.style,
            enable_thinking=bool(get_translate_settings(db, settings).get("openai_enable_thinking")),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return TranslateTestResponse(translated_text=translated)


@app.get("/subtitle/models", response_model=list[WhisperModelInfo])
def list_whisper_models(settings: SubtitleServiceSettings = Depends(get_settings)) -> list[WhisperModelInfo]:
    root = _models_dir(settings)
    root.mkdir(parents=True, exist_ok=True)

    out: list[WhisperModelInfo] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if not p.is_dir():
            continue
        if p.name.startswith("."):
            continue
        out.append(WhisperModelInfo(name=p.name, path=str(p), size_bytes=_dir_size_bytes(p)))
    return out


@app.post("/subtitle/models/download", response_model=WhisperModelInfo)
def download_whisper_model(
    payload: WhisperModelDownloadRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> WhisperModelInfo:
    try:
        engine = normalize_model_download_engine(payload.engine)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    model = (payload.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    name = payload.name
    if not name:
        try:
            name = default_model_dir_name(engine, model)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    name = _validate_model_name(name)

    # Use the dedicated model-download proxy (stored in DB via Settings · ASR).
    asr_cfg = get_asr_settings(db, settings)
    proxy = str(asr_cfg.get("model_download_proxy") or "").strip() or None
    try:
        dest = download_model_snapshot(
            engine=engine,
            model=model,
            model_dir=_models_dir(settings),
            name=name,
            revision=payload.revision,
            force=payload.force,
            proxy=proxy,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"download failed: {type(e).__name__}: {e}") from e

    return WhisperModelInfo(name=name, path=str(dest), size_bytes=_dir_size_bytes(dest))


@app.post("/subtitle/models/upload", response_model=WhisperModelInfo)
async def upload_whisper_model(
    name: str,
    file: UploadFile = File(...),
    settings: SubtitleServiceSettings = Depends(get_settings),
) -> WhisperModelInfo:
    name = _validate_model_name(name)
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="only .zip is supported")

    dest = _models_dir(settings) / name
    if dest.exists():
        raise HTTPException(status_code=400, detail="model already exists; delete it first")

    max_zip_bytes = _WHISPER_MODEL_ZIP_MAX_BYTES
    read_bytes = 0
    with tempfile.NamedTemporaryFile(prefix="whisper_model_", suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > max_zip_bytes:
                    raise UploadTooLargeError(f"model zip too large: max {max_zip_bytes} bytes")
                tmp.write(chunk)
        except UploadTooLargeError as e:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail=str(e)) from e

    try:
        _safe_extract_zip(
            tmp_path,
            dest,
            max_files=_WHISPER_MODEL_ZIP_MAX_FILES,
            max_uncompressed_bytes=_WHISPER_MODEL_ZIP_MAX_UNCOMPRESSED_BYTES,
        )
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e)) from e
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    return WhisperModelInfo(name=name, path=str(dest), size_bytes=_dir_size_bytes(dest))


@app.delete("/subtitle/models/{name}")
def delete_whisper_model(name: str, settings: SubtitleServiceSettings = Depends(get_settings)) -> dict[str, bool]:
    name = _validate_model_name(name)
    dest = _models_dir(settings) / name
    if not dest.exists():
        raise HTTPException(status_code=404, detail="model not found")
    shutil.rmtree(dest, ignore_errors=True)
    return {"deleted": True}


@app.post("/subtitle/models/proxy/test", response_model=ModelDownloadProxyTestResponse)
def test_model_download_proxy(
    payload: ModelDownloadProxyTestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ModelDownloadProxyTestResponse:
    url = str(payload.url or "").strip() or "https://huggingface.co/robots.txt"
    try:
        parsed_url = urlsplit(url)
    except ValueError:
        parsed_url = None
    hostname = str(parsed_url.hostname or "").rstrip(".").lower() if parsed_url is not None else ""
    if (
        parsed_url is None
        or parsed_url.scheme.lower() not in {"http", "https"}
        or parsed_url.username is not None
        or parsed_url.password is not None
        or not (hostname == "huggingface.co" or hostname.endswith(".huggingface.co"))
    ):
        return ModelDownloadProxyTestResponse(
            ok=False,
            url=url,
            used_proxy=str(payload.proxy or "").strip() or None,
            status_code=None,
            elapsed_ms=0,
            error="model proxy test URL must be an HTTP(S) huggingface.co URL",
        )

    if payload.proxy is not None:
        proxy = str(payload.proxy or "").strip()
    else:
        cfg = get_asr_settings(db, settings)
        proxy = str(cfg.get("model_download_proxy") or "").strip()

    start = time.perf_counter()
    client_kwargs: dict[str, Any] = {"timeout": 20.0, "follow_redirects": False}
    if proxy:
        try:
            client_kwargs["proxy"] = proxy
        except Exception:
            pass

    try:
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.get(url)
                ok = resp.status_code < 400
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return ModelDownloadProxyTestResponse(
                    ok=ok,
                    url=url,
                    used_proxy=proxy or None,
                    status_code=resp.status_code,
                    elapsed_ms=elapsed_ms,
                )
        except TypeError:
            with httpx.Client(timeout=20.0, follow_redirects=False) as client:
                resp = client.get(url)
                ok = resp.status_code < 400
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return ModelDownloadProxyTestResponse(
                    ok=ok,
                    url=url,
                    used_proxy=None,
                    status_code=resp.status_code,
                    elapsed_ms=elapsed_ms,
                    error=HTTPX_PROXY_KWARG_UNSUPPORTED,
                )
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ModelDownloadProxyTestResponse(
            ok=False,
            url=url,
            used_proxy=proxy or None,
            status_code=None,
            elapsed_ms=elapsed_ms,
            error=format_httpx_proxy_error(e, proxy=proxy),
        )


@app.post("/subtitle/jobs")
def create_job(payload: SubtitleJobCreate, db: Session = Depends(get_db)) -> dict[str, str]:
    # Publishing takes the same task-row lock. Recheck the state here even when
    # the orchestrator already validated it before its internal HTTP request.
    task = db.get(Task, payload.task_id, with_for_update=True, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status == TaskStatus.published:
        raise HTTPException(status_code=409, detail="task is already published; create a new task to generate subtitles")
    if task.status == TaskStatus.canceled:
        raise HTTPException(status_code=409, detail="task is stopped; resume it before submitting subtitle work")

    # Re-check while holding the task row lock. The orchestrator-side check is
    # only an optimization; without this guard two concurrent requests can both
    # observe an empty queue before either insert becomes visible.
    active_job = (
        db.query(SubtitleJob)
        .filter(
            SubtitleJob.task_id == payload.task_id,
            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
        )
        .order_by(SubtitleJob.created_at.asc(), SubtitleJob.id.asc())
        .first()
    )
    if active_job is not None:
        return {"job_id": str(active_job.id), "status": active_job.status.value}

    request_json = payload.model_dump(mode="json")
    if "runtime_profile" not in payload.model_fields_set or payload.runtime_profile is None:
        # Legacy automatic jobs have no explicit marker. Infer True only for
        # those tasks; new manual orchestrator requests send False explicitly.
        request_json["runtime_profile"] = parse_auto_youtube_created_by(task.created_by) is not None
    if "youtube_subtitle_mode" not in payload.model_fields_set:
        request_json["youtube_subtitle_mode"] = "target" if payload.prefer_youtube_subtitles else "off"
    request_json["prefer_youtube_subtitles"] = request_json.get("youtube_subtitle_mode") != "off"

    job = SubtitleJob(task_id=payload.task_id, request_json=request_json)
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"job_id": str(job.id), "status": job.status.value}


@app.get("/subtitle/jobs/{job_id}", response_model=SubtitleJobRead)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> SubtitleJobRead:
    job = db.get(SubtitleJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    artifacts = (
        db.query(Asset)
        .filter(
            Asset.task_id == job.task_id,
            Asset.kind.in_(
                [
                    AssetKind.audio_wav,
                    AssetKind.segments_json,
                    AssetKind.subtitle_srt,
                    AssetKind.subtitle_ass,
                    AssetKind.video_final,
                    AssetKind.log,
                ]
            ),
        )
        .order_by(Asset.created_at.asc())
        .all()
    )

    return SubtitleJobRead(
        job_id=job.id,
        task_id=job.task_id,
        status=job.status.value,
        progress=job.progress,
        artifacts=[{"kind": a.kind.value, "key": a.storage_key} for a in artifacts],
        logs_key=job.logs_key,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _clamp_queue_limit(v: int) -> int:
    v = int(v or 200)
    if v < 1:
        return 1
    if v > 2000:
        return 2000
    return v


def _subtitle_worker_slots() -> int:
    try:
        value = int(os.getenv("HATCHET_SUBTITLE_WORKER_SLOTS") or "1")
    except (TypeError, ValueError):
        value = 1
    return max(1, min(256, value))


def _auto_youtube_queue_progress(db: Session, task: Task) -> tuple[str, int, str | None] | None:
    if task.source_type != SourceType.youtube or parse_auto_youtube_created_by(task.created_by) is None:
        return None
    try:
        from videoroll.apps.orchestrator_api.services.youtube_service import get_download_progress

        data = get_download_progress(
            task.id,
            db=db,
            redis_url=str(get_subtitle_settings().redis_url or ""),
        )
    except Exception:
        logger.exception("failed to read YouTube download progress for task %s", task.id)
        return None

    status = str(data.get("status") or "").strip().lower()
    if not status or status == "idle":
        return None
    try:
        progress = max(0, min(100, int(data.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0
    error = str(data.get("error") or "").strip() or None
    if status == "completed":
        return "subtitle_handoff", 100, error
    return "youtube_download", progress, error


def _read_task_queue(db: Session, *, limit: int) -> TaskQueueRead:
    """Project the active execution queue from durable job state.

    Hatchet owns scheduling. This projection only reads durable business job
    state so the UI cannot report synthetic queue states from the removed
    Celery scheduler.
    """
    limit = _clamp_queue_limit(limit)
    live_task = Task.status.notin_([TaskStatus.canceled, TaskStatus.published])

    subtitle_by_task: dict[uuid.UUID, SubtitleJob] = {}
    for job in (
        db.query(SubtitleJob)
        .join(Task, Task.id == SubtitleJob.task_id)
        .filter(SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]), live_task)
        .order_by(SubtitleJob.updated_at.desc(), SubtitleJob.created_at.desc())
        .limit(5000)
        .all()
    ):
        subtitle_by_task.setdefault(job.task_id, job)

    render_by_task: dict[uuid.UUID, RenderJob] = {}
    for job in (
        db.query(RenderJob)
        .join(Task, Task.id == RenderJob.task_id)
        .filter(RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]), live_task)
        .order_by(RenderJob.updated_at.desc(), RenderJob.created_at.desc())
        .limit(5000)
        .all()
    ):
        render_by_task.setdefault(job.task_id, job)

    candidate_ids = set(subtitle_by_task) | set(render_by_task)
    auto_tasks = (
        db.query(Task)
        .filter(
            Task.source_type == SourceType.youtube,
            Task.status.in_([TaskStatus.ingested, TaskStatus.downloaded]),
            live_task,
        )
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(5000)
        .all()
    )
    for task in auto_tasks:
        if parse_auto_youtube_created_by(task.created_by) is not None:
            candidate_ids.add(task.id)

    if not candidate_ids:
        return TaskQueueRead(
            settings=TaskQueueSettingsRead(scheduler="hatchet", subtitle_worker_slots=_subtitle_worker_slots()),
            running_count=0,
            queued_count=0,
            tasks=[],
        )

    tasks = (
        db.query(Task)
        .filter(Task.id.in_(candidate_ids), live_task)
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(limit)
        .all()
    )

    items: list[TaskQueueItemRead] = []
    for task in tasks:
        sj = subtitle_by_task.get(task.id)
        rj = render_by_task.get(task.id)
        state = "queued"
        stage = "workflow"
        progress = 0
        error_message = task.error_message
        created_at = task.created_at
        updated_at = task.updated_at
        subtitle_job_id = sj.id if sj is not None else None
        render_job_id = rj.id if rj is not None else None

        if rj is not None:
            state = "running" if rj.status == RenderJobStatus.running else "queued"
            stage = "render" if state == "running" else "waiting_render"
            progress = int(rj.progress or 0)
            error_message = rj.error_message
            created_at = rj.created_at
            updated_at = rj.updated_at
            subtitle_job_id = rj.subtitle_job_id or subtitle_job_id
        elif sj is not None:
            state = "running" if sj.status == SubtitleJobStatus.running else "queued"
            stage = "subtitle"
            progress = int(sj.progress or 0)
            error_message = sj.error_message
            created_at = sj.created_at
            updated_at = sj.updated_at
        else:
            youtube_progress = _auto_youtube_queue_progress(db, task)
            if youtube_progress is not None:
                stage, progress, error_message = youtube_progress
                state = "running" if stage == "youtube_download" else "queued"

        items.append(
            TaskQueueItemRead(
                task_id=task.id,
                state=state,
                stage=stage,
                priority=int(task.priority or 0),
                subtitle_job_id=subtitle_job_id,
                render_job_id=render_job_id,
                progress=progress,
                error_message=error_message,
                created_at=created_at,
                updated_at=updated_at,
            )
        )

    items.sort(key=lambda item: (0 if item.state == "running" else 1, -int(item.priority or 0), item.created_at))
    return TaskQueueRead(
        settings=TaskQueueSettingsRead(scheduler="hatchet", subtitle_worker_slots=_subtitle_worker_slots()),
        running_count=sum(1 for item in items if item.state == "running"),
        queued_count=sum(1 for item in items if item.state == "queued"),
        tasks=items,
    )


@app.get("/subtitle/task_queue", response_model=TaskQueueRead)
def get_task_queue(limit: int = 200, db: Session = Depends(get_db)) -> TaskQueueRead:
    return _read_task_queue(db, limit=limit)
