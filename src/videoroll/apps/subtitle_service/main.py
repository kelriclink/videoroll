from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError

from videoroll.ai.client import openai_chat_config_from_settings
from videoroll.ai.service import translate_text_openai
from videoroll.config import SubtitleServiceSettings, get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.auto_migrate import auto_migrate
from videoroll.db.models import Asset, AssetKind, RenderJob, RenderJobStatus, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.db.session import db_session, get_engine
from videoroll.storage.s3 import S3Store
from videoroll.apps.subtitle_service.schemas import (
    ASRDefaultsRead,
    ASRDefaultsUpdate,
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
    KnowledgeItemUpsertRequest,
    KnowledgeItemUpsertResponse,
    AgentRunRead,
    EmbeddingModelDownloadRequest,
    EmbeddingModelListRequest,
    EmbeddingModelInfo,
    EmbeddingTestRequest,
    EmbeddingTestResponse,
    TaskQueueItemRead,
    TaskQueueRead,
    TaskQueueSettingsRead,
    TaskQueueSettingsUpdate,
)
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings, update_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile, update_auto_profile
from videoroll.apps.subtitle_service.model_downloads import (
    default_model_dir_name,
    download_model_snapshot,
    normalize_model_download_engine,
)
from videoroll.apps.subtitle_service.render_queue_store import get_task_queue_settings, update_task_queue_settings
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
    list_agent_runs,
    rebuild_knowledge_embeddings,
    list_knowledge_items,
    rag_settings_from_translate_settings,
    upsert_knowledge_item,
)
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings, update_translate_settings
from videoroll.apps.subtitle_service.worker_concurrency import sync_subtitle_worker_concurrency_for_task_queue_settings
from videoroll.apps.subtitle_service.worker import TASK_QUEUE_LOCK_OWNER, celery_app
from videoroll.utils.auto_youtube import parse_auto_youtube_created_by
from videoroll.utils.cpu import process_cpu_count
from videoroll.utils.httpx_proxy import HTTPX_PROXY_KWARG_UNSUPPORTED, format_httpx_proxy_error
from videoroll.utils.intel_gpu import detect_intel_hardware

logger = logging.getLogger(__name__)


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
            ]
        )
    )


def _ensure_rag_schema(settings: SubtitleServiceSettings) -> None:
    try:
        auto_migrate(settings.database_url, force=True)
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


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
                raise HTTPException(status_code=400, detail=f"unsafe zip entry: {name}")
        zf.extractall(dest_dir)


app = FastAPI(title="videoroll-subtitle-service", version="0.1.0")

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


@app.on_event("startup")
def _startup() -> None:
    settings = get_subtitle_settings()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    auto_migrate(settings.database_url)
    S3Store(settings).ensure_bucket()
    _models_dir(settings).mkdir(parents=True, exist_ok=True)


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
        rag_wiki_enabled=cfg["rag_wiki_enabled"],
        rag_search_enabled=cfg["rag_search_enabled"],
        rag_search_url=cfg["rag_search_url"],
        rag_domain=cfg["rag_domain"],
        rag_agent_parallelism=cfg["rag_agent_parallelism"],
        rag_agent_timeout_seconds=cfg["rag_agent_timeout_seconds"],
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
        rag_wiki_enabled=cfg["rag_wiki_enabled"],
        rag_search_enabled=cfg["rag_search_enabled"],
        rag_search_url=cfg["rag_search_url"],
        rag_domain=cfg["rag_domain"],
        rag_agent_parallelism=cfg["rag_agent_parallelism"],
        rag_agent_timeout_seconds=cfg["rag_agent_timeout_seconds"],
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


@app.get("/subtitle/agents/runs", response_model=list[AgentRunRead])
def list_agent_runs_view(
    status: str | None = None,
    limit: int = 30,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> list[AgentRunRead]:
    try:
        rows = list_agent_runs(db, status=status, limit=limit)
    except Exception as e:
        db.rollback()
        if not _is_missing_knowledge_table_error(e):
            _handle_knowledge_db_error(e, settings)
        _ensure_rag_schema(settings)
        try:
            rows = list_agent_runs(db, status=status, limit=limit)
        except Exception as retry_error:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"RAG agent tables are not ready: {retry_error}") from retry_error
    return [AgentRunRead(**row) for row in rows]


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
        cfg = get_translate_settings(db, settings)
        translated = translate_text_openai(
            text,
            target_lang=payload.target_lang,
            style=payload.style,
            config=openai_chat_config_from_settings(cfg),
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

    with tempfile.NamedTemporaryFile(prefix="whisper_model_", suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)

    try:
        _safe_extract_zip(tmp_path, dest)
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

    if payload.proxy is not None:
        proxy = str(payload.proxy or "").strip()
    else:
        cfg = get_asr_settings(db, settings)
        proxy = str(cfg.get("model_download_proxy") or "").strip()

    start = time.perf_counter()
    client_kwargs: dict[str, Any] = {"timeout": 20.0, "follow_redirects": True}
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
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
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
    request_json = payload.model_dump(mode="json")
    if "youtube_subtitle_mode" not in payload.model_fields_set:
        request_json["youtube_subtitle_mode"] = "target" if payload.prefer_youtube_subtitles else "off"
    request_json["prefer_youtube_subtitles"] = request_json.get("youtube_subtitle_mode") != "off"

    job = SubtitleJob(task_id=payload.task_id, request_json=request_json)
    db.add(job)
    db.commit()
    db.refresh(job)
    celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
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


def _read_task_queue(db: Session, *, limit: int) -> TaskQueueRead:
    limit = _clamp_queue_limit(limit)
    cfg = get_task_queue_settings(db)
    now = datetime.now(tz=timezone.utc)

    locked_q = db.query(Task).filter(Task.lock_owner == TASK_QUEUE_LOCK_OWNER, Task.lock_until.is_not(None), Task.lock_until > now)
    running_count = int(locked_q.count() or 0)
    locked = locked_q.order_by(Task.lock_until.asc()).limit(limit).all()
    locked_ids = [t.id for t in locked]

    render_running_by_task: dict[uuid.UUID, RenderJob] = {}
    subtitle_running_by_task: dict[uuid.UUID, SubtitleJob] = {}
    render_queued_by_task: dict[uuid.UUID, RenderJob] = {}
    subtitle_queued_by_task: dict[uuid.UUID, SubtitleJob] = {}

    if locked_ids:
        for rj in (
            db.query(RenderJob)
            .filter(RenderJob.task_id.in_(locked_ids), RenderJob.status == RenderJobStatus.running)
            .order_by(RenderJob.started_at.desc().nullslast(), RenderJob.updated_at.desc(), RenderJob.created_at.desc())
            .all()
        ):
            render_running_by_task.setdefault(rj.task_id, rj)
        for sj in (
            db.query(SubtitleJob)
            .filter(SubtitleJob.task_id.in_(locked_ids), SubtitleJob.status == SubtitleJobStatus.running)
            .order_by(SubtitleJob.updated_at.desc(), SubtitleJob.created_at.desc())
            .all()
        ):
            subtitle_running_by_task.setdefault(sj.task_id, sj)
        for rj in (
            db.query(RenderJob)
            .filter(RenderJob.task_id.in_(locked_ids), RenderJob.status == RenderJobStatus.queued)
            .order_by(RenderJob.created_at.asc())
            .all()
        ):
            render_queued_by_task.setdefault(rj.task_id, rj)
        for sj in (
            db.query(SubtitleJob)
            .filter(SubtitleJob.task_id.in_(locked_ids), SubtitleJob.status == SubtitleJobStatus.queued)
            .order_by(SubtitleJob.created_at.asc())
            .all()
        ):
            subtitle_queued_by_task.setdefault(sj.task_id, sj)

    running_items: list[TaskQueueItemRead] = []
    for t in locked:
        tid = t.id
        r_run = render_running_by_task.get(tid)
        s_run = subtitle_running_by_task.get(tid)
        r_q = render_queued_by_task.get(tid)
        s_q = subtitle_queued_by_task.get(tid)

        if r_run:
            running_items.append(
                TaskQueueItemRead(
                    task_id=tid,
                    state="running",
                    stage="render",
                    render_job_id=r_run.id,
                    subtitle_job_id=r_run.subtitle_job_id,
                    progress=int(r_run.progress or 0),
                    error_message=r_run.error_message,
                    created_at=r_run.created_at,
                    updated_at=r_run.updated_at,
                )
            )
            continue
        if s_run:
            running_items.append(
                TaskQueueItemRead(
                    task_id=tid,
                    state="running",
                    stage="subtitle",
                    subtitle_job_id=s_run.id,
                    progress=int(s_run.progress or 0),
                    error_message=s_run.error_message,
                    created_at=s_run.created_at,
                    updated_at=s_run.updated_at,
                )
            )
            continue
        if r_q:
            running_items.append(
                TaskQueueItemRead(
                    task_id=tid,
                    state="running",
                    stage="waiting_render",
                    render_job_id=r_q.id,
                    subtitle_job_id=r_q.subtitle_job_id,
                    progress=int(r_q.progress or 0),
                    error_message=r_q.error_message,
                    created_at=r_q.created_at,
                    updated_at=r_q.updated_at,
                )
            )
            continue
        if s_q:
            running_items.append(
                TaskQueueItemRead(
                    task_id=tid,
                    state="running",
                    stage="waiting_subtitle",
                    subtitle_job_id=s_q.id,
                    progress=int(s_q.progress or 0),
                    error_message=s_q.error_message,
                    created_at=s_q.created_at,
                    updated_at=s_q.updated_at,
                )
            )
            continue

        running_items.append(
            TaskQueueItemRead(
                task_id=tid,
                state="running",
                stage="idle",
                progress=0,
                error_message=None,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
        )

    unlocked = (Task.lock_owner != TASK_QUEUE_LOCK_OWNER) | (Task.lock_until.is_(None)) | (Task.lock_until <= now)
    orphaned_render_by_task: dict[uuid.UUID, RenderJob] = {}
    orphaned_subtitle_by_task: dict[uuid.UUID, SubtitleJob] = {}
    for rj in (
        db.query(RenderJob)
        .join(Task, Task.id == RenderJob.task_id)
        .filter(RenderJob.status == RenderJobStatus.running, unlocked)
        .order_by(RenderJob.updated_at.asc(), RenderJob.created_at.asc())
        .limit(5000)
        .all()
    ):
        orphaned_render_by_task.setdefault(rj.task_id, rj)
    for sj in (
        db.query(SubtitleJob)
        .join(Task, Task.id == SubtitleJob.task_id)
        .filter(SubtitleJob.status == SubtitleJobStatus.running, unlocked)
        .order_by(SubtitleJob.updated_at.asc(), SubtitleJob.created_at.asc())
        .limit(5000)
        .all()
    ):
        orphaned_subtitle_by_task.setdefault(sj.task_id, sj)

    # queued_count: distinct tasks with queued jobs, plus orphaned running jobs that lost their task lock.
    queued_task_ids: set[uuid.UUID] = set(orphaned_render_by_task) | set(orphaned_subtitle_by_task)
    for tid, in (
        db.query(SubtitleJob.task_id)
        .join(Task, Task.id == SubtitleJob.task_id)
        .filter(SubtitleJob.status == SubtitleJobStatus.queued, unlocked)
        .distinct()
        .limit(5000)
        .all()
    ):
        queued_task_ids.add(tid)
    for tid, in (
        db.query(RenderJob.task_id)
        .join(Task, Task.id == RenderJob.task_id)
        .filter(RenderJob.status == RenderJobStatus.queued, unlocked)
        .distinct()
        .limit(5000)
        .all()
    ):
        queued_task_ids.add(tid)

    recoverable_pipeline_tasks: list[Task] = []
    bootstrap_cutoff = now - timedelta(seconds=60)
    for task in (
        db.query(Task)
        .filter(
            Task.source_type == SourceType.youtube,
            Task.status.in_([TaskStatus.ingested, TaskStatus.downloaded]),
            unlocked,
            Task.updated_at.is_not(None),
            Task.updated_at < bootstrap_cutoff,
        )
        .order_by(Task.updated_at.asc(), Task.created_at.asc())
        .limit(5000)
        .all()
    ):
        if parse_auto_youtube_created_by(task.created_by) is None:
            continue
        has_jobs = (
            db.query(SubtitleJob).filter(SubtitleJob.task_id == task.id).count()
            + db.query(RenderJob).filter(RenderJob.task_id == task.id).count()
        )
        if has_jobs:
            continue
        recoverable_pipeline_tasks.append(task)
        queued_task_ids.add(task.id)

    queued_count = len(queued_task_ids)

    remaining = max(0, limit - len(running_items))
    queued_items: list[TaskQueueItemRead] = []
    seen: set[uuid.UUID] = set()

    for rj in orphaned_render_by_task.values():
        if rj.task_id in seen:
            continue
        seen.add(rj.task_id)
        queued_items.append(
            TaskQueueItemRead(
                task_id=rj.task_id,
                state="queued",
                stage="recover_render",
                render_job_id=rj.id,
                subtitle_job_id=rj.subtitle_job_id,
                progress=int(rj.progress or 0),
                error_message=rj.error_message,
                created_at=rj.created_at,
                updated_at=rj.updated_at,
            )
        )
        if len(queued_items) >= remaining:
            break

    if len(queued_items) < remaining:
        for sj in orphaned_subtitle_by_task.values():
            if sj.task_id in seen:
                continue
            seen.add(sj.task_id)
            queued_items.append(
                TaskQueueItemRead(
                    task_id=sj.task_id,
                    state="queued",
                    stage="recover_subtitle",
                    subtitle_job_id=sj.id,
                    progress=int(sj.progress or 0),
                    error_message=sj.error_message,
                    created_at=sj.created_at,
                    updated_at=sj.updated_at,
                )
            )
            if len(queued_items) >= remaining:
                break

    fetch_n = min(5000, max(50, remaining * 20))
    if len(queued_items) < remaining:
        for task in recoverable_pipeline_tasks:
            if task.id in seen:
                continue
            seen.add(task.id)
            queued_items.append(
                TaskQueueItemRead(
                    task_id=task.id,
                    state="queued",
                    stage="recover_pipeline",
                    progress=0,
                    error_message=task.error_message,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )
            if len(queued_items) >= remaining:
                break

    if len(queued_items) < remaining:
        for sj in (
            db.query(SubtitleJob)
            .join(Task, Task.id == SubtitleJob.task_id)
            .filter(SubtitleJob.status == SubtitleJobStatus.queued, unlocked)
            .order_by(SubtitleJob.created_at.asc())
            .limit(fetch_n)
            .all()
        ):
            if sj.task_id in seen:
                continue
            seen.add(sj.task_id)
            queued_items.append(
                TaskQueueItemRead(
                    task_id=sj.task_id,
                    state="queued",
                    stage="subtitle",
                    subtitle_job_id=sj.id,
                    progress=int(sj.progress or 0),
                    error_message=sj.error_message,
                    created_at=sj.created_at,
                    updated_at=sj.updated_at,
                )
            )
            if len(queued_items) >= remaining:
                break

    if len(queued_items) < remaining:
        for rj in (
            db.query(RenderJob)
            .join(Task, Task.id == RenderJob.task_id)
            .filter(RenderJob.status == RenderJobStatus.queued, unlocked)
            .order_by(RenderJob.created_at.asc())
            .limit(fetch_n)
            .all()
        ):
            if rj.task_id in seen:
                continue
            seen.add(rj.task_id)
            queued_items.append(
                TaskQueueItemRead(
                    task_id=rj.task_id,
                    state="queued",
                    stage="render",
                    render_job_id=rj.id,
                    subtitle_job_id=rj.subtitle_job_id,
                    progress=int(rj.progress or 0),
                    error_message=rj.error_message,
                    created_at=rj.created_at,
                    updated_at=rj.updated_at,
                )
            )
            if len(queued_items) >= remaining:
                break

    return TaskQueueRead(
        settings=TaskQueueSettingsRead(**cfg),
        running_count=running_count,
        queued_count=int(queued_count),
        tasks=[*running_items, *queued_items],
    )


@app.get("/subtitle/task_queue", response_model=TaskQueueRead)
def get_task_queue(limit: int = 200, db: Session = Depends(get_db)) -> TaskQueueRead:
    return _read_task_queue(db, limit=limit)


@app.get("/subtitle/render_queue", response_model=TaskQueueRead)
def get_render_queue_legacy(limit: int = 200, db: Session = Depends(get_db)) -> TaskQueueRead:
    return _read_task_queue(db, limit=limit)


@app.put("/subtitle/task_queue/settings", response_model=TaskQueueSettingsRead)
def put_task_queue_settings_view(payload: TaskQueueSettingsUpdate, db: Session = Depends(get_db)) -> TaskQueueSettingsRead:
    cfg = update_task_queue_settings(db, payload.model_dump(exclude_unset=True))
    runtime_sync = sync_subtitle_worker_concurrency_for_task_queue_settings(celery_app, cfg, queue="subtitle")
    if not bool(runtime_sync.get("ok")):
        logger.warning(
            "subtitle worker concurrency runtime sync incomplete after task queue update: %s",
            runtime_sync.get("detail"),
        )
    celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
    return TaskQueueSettingsRead(
        **cfg,
        runtime_worker_concurrency=runtime_sync.get("target_concurrency"),
        runtime_sync_ok=bool(runtime_sync.get("ok")),
        runtime_sync_detail=str(runtime_sync.get("detail") or "").strip() or None,
        runtime_sync_workers=list(runtime_sync.get("workers") or []),
    )


@app.put("/subtitle/render_queue/settings", response_model=TaskQueueSettingsRead)
def put_render_queue_settings_legacy(payload: TaskQueueSettingsUpdate, db: Session = Depends(get_db)) -> TaskQueueSettingsRead:
    return put_task_queue_settings_view(payload, db=db)


@app.post("/subtitle/task_queue/tick")
def post_task_queue_tick() -> dict[str, str]:
    """
    Best-effort scheduler kick.
    Useful when a job is queued but no tick was delivered/consumed.
    """
    celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
    return {"status": "queued"}


@app.post("/subtitle/render_queue/tick")
def post_render_queue_tick_legacy() -> dict[str, str]:
    return post_task_queue_tick()
