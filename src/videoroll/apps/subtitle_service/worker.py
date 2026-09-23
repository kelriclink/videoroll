from __future__ import annotations

import contextlib
import hashlib
import httpx
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from celery import Celery
from celery.exceptions import Retry
from celery.signals import worker_init
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from videoroll.ai.service import AIService
from videoroll.ai.usage import reset_ai_usage_context, set_ai_usage_context
from videoroll.config import get_orchestrator_settings, get_subtitle_settings
from videoroll.db.migrate import initialize_database
from videoroll.db.models import (
    Asset,
    AssetKind,
    PipelineRun,
    PublishBatch,
    PublishJob,
    PublishState,
    RenderJob,
    RenderJobStatus,
    SourceType,
    Subtitle,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_sessionmaker
from videoroll.realtime import publish_log_updated, publish_queue_changed
from videoroll.storage.filesystem import FileStore
from videoroll.apps.security.service_auth import INTERNAL_TOKEN_HEADER, service_token
from videoroll.utils.auto_youtube import parse_auto_youtube_created_by
from videoroll.utils.hashing import sha256_file
from videoroll.apps.subtitle_service.processing import (
    Segment,
    convert_subtitle_to_srt,
    extract_audio,
    mux_soft_sub,
    probe_video_resolution,
    reconcile_overlapping_asr_segments,
    render_burn_in,
    srt_to_segments,
    segments_from_json_data,
    segments_to_ass,
    segments_to_json_data,
    transcribe_external_whisper,
    transcribe_groq_whisper,
    transcribe_cloudflare_workers_ai,
    transcribe_faster_whisper,
    transcribe_mock,
    transcribe_openvino_whisper,
    write_json,
)
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.subtitle_service.model_downloads import (
    default_model_dir_name,
    download_model_snapshot,
    resolve_model_repo_id,
)
from videoroll.apps.subtitle_service.rag import translation_trace_recorder
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.apps.subtitle_service.translation_checkpoint import TranslationCheckpointStore
from videoroll.apps.subtitle_service.translation_stage import (
    TranslationRetryRequired,
    run_translation_stage,
)
from videoroll.apps.subtitle_service.translation_postprocess import postprocess_translation
from videoroll.apps.subtitle_service.subtitle_finalization import (
    build_render_job_payload,
    complete_subtitle_handoff,
    mark_subtitle_ready,
    persist_subtitle_outputs,
    store_ass_output,
)
from videoroll.apps.outbox.dispatcher import dispatch_outbox_events
from videoroll.apps.outbox.service import create_outbox_event
from videoroll.apps.outbox.worker_inbox import (
    claim_outbox_operation,
    finish_operation,
    release_operation,
)
from videoroll.apps.publish_lifecycle import (
    current_publish_batches_for_task,
    enqueue_publish_batch_cleanup,
    mark_publish_batch_cleanup_enqueued,
)
from videoroll.apps.orchestrator_api.youtube_downloader import (
    download_youtube_subtitle,
    extract_youtube_metadata,
    normalize_youtube_subtitle_mode,
    pick_preferred_youtube_subtitle,
)
from videoroll.apps.subtitle_service.queues import SUBTITLE_CONTROL_QUEUE
from videoroll.apps.subtitle_service.worker_concurrency import (
    JobLeaseHeartbeat,
    acquire_job_lease,
    release_job_lease,
)
from videoroll.apps.youtube_settings_store import (
    get_youtube_cookies_txt,
    get_youtube_settings,
    normalize_and_validate_netscape_cookies_txt,
)
from videoroll.utils.cpu import process_cpu_count


def _unique_storage_key(prefix: str, digest: str, suffix: str) -> str:
    return f"{prefix}_{digest[:16]}_{uuid.uuid4().hex[:12]}{suffix}"


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except Exception:
        value = default
    return max(1, value)


def _uses_runtime_auto_profile(task: Task, request_json: dict[str, Any], *nested: dict[str, Any]) -> bool:
    """Resolve runtime-box policy while preserving explicit manual overrides.

    New requests carry runtime_profile explicitly. Legacy automatic backlog rows
    predate that field, so only those fall back to task.created_by.
    """
    if "runtime_profile" in request_json and request_json.get("runtime_profile") is not None:
        return bool(request_json.get("runtime_profile"))
    for item in nested:
        if "runtime_profile" in item and item.get("runtime_profile") is not None:
            return bool(item.get("runtime_profile"))
    return parse_auto_youtube_created_by(task.created_by) is not None


def _active_pipeline_job(db: Session, task_id: uuid.UUID) -> tuple[str, uuid.UUID] | None:
    """Return existing active work so pipeline recovery never creates a duplicate stage."""
    subtitle = (
        db.query(SubtitleJob)
        .filter(
            SubtitleJob.task_id == task_id,
            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
        )
        .order_by(SubtitleJob.created_at.desc())
        .first()
    )
    if subtitle is not None:
        return "subtitle", subtitle.id
    render = (
        db.query(RenderJob)
        .filter(
            RenderJob.task_id == task_id,
            RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
        )
        .order_by(RenderJob.created_at.desc())
        .first()
    )
    if render is not None:
        return "render", render.id
    return None


settings = get_subtitle_settings()
logger = logging.getLogger(__name__)
_DB_READY_LOCK = threading.Lock()
_DB_READY_PID: int | None = None
_TASK_QUEUE_TICK_INTERVAL_SECONDS = _positive_int_env("TASK_QUEUE_TICK_INTERVAL_SECONDS", 10)


def _orchestrator_internal_headers() -> dict[str, str]:
    """Derive the service credential at the side-effect boundary.

    Importing a worker is not a service start and must remain possible for
    offline tooling and tests.  A real worker start and every orchestrator
    request still validate the dedicated production secret fail-closed.
    """
    return {INTERNAL_TOKEN_HEADER: service_token(settings)}


celery_app = Celery("subtitle_service", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Subtitle/render tasks are long-lived. Reserving four tasks per process
    # makes one worker hoard dozens of jobs and amplifies restart recovery.
    worker_prefetch_multiplier=1,
    # Recycle children periodically so cached native models/allocators do not
    # accumulate indefinitely across many completed tasks.
    worker_max_tasks_per_child=settings.celery_sub_max_tasks_per_child,
    beat_schedule={
        "subtitle-service-publish-cleanup-retry": {
            "task": "subtitle_service.enqueue_pending_publish_batch_cleanups",
            "schedule": 60.0,
            "args": (),
            "options": {"queue": SUBTITLE_CONTROL_QUEUE},
        },
        "subtitle-service-outbox-dispatch": {
            "task": "subtitle_service.dispatch_outbox",
            "schedule": 5.0,
            "args": (),
            "options": {"queue": SUBTITLE_CONTROL_QUEUE},
        },
        "subtitle-service-publish-dispatch-recovery": {
            "task": "subtitle_service.recover_publish_dispatches",
            "schedule": 30.0,
            "args": (),
            "options": {"queue": SUBTITLE_CONTROL_QUEUE},
        },
    },
)

def _resolve_faster_whisper_model(model_name: str, model_dir: Path, *, proxy: str | None = None) -> str:
    model_name = (model_name or "").strip()
    if not model_name:
        return model_name

    # If user provided a local path, use it directly.
    p = Path(model_name)
    if p.exists():
        return str(p)

    # Prefer our persisted models dir so downloads survive container rebuilds.
    repo_id = resolve_model_repo_id("faster-whisper", model_name)
    local_name = default_model_dir_name("faster-whisper", model_name)
    dest = model_dir / local_name
    if dest.exists():
        return str(dest)

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception:
        # Fall back to faster-whisper's own downloader/cache.
        return repo_id
    try:
        dest = download_model_snapshot(
            engine="faster-whisper",
            model=model_name,
            model_dir=model_dir,
            name=local_name,
            proxy=proxy,
        )
    except Exception as e:
        raise RuntimeError(
            f"failed to download whisper model '{repo_id}' into '{dest}'. "
            "Open Settings → ASR/Whisper to download it first, or set asr.model to an existing path. "
            f"detail={type(e).__name__}: {e}"
        ) from e

    return str(dest)


def _resolve_openvino_model(model_name: str, model_dir: Path, *, proxy: str | None = None) -> str:
    model_name = (model_name or "").strip()
    if not model_name:
        raise RuntimeError(
            "OpenVINO ASR model is empty. Set SUBTITLE_OPENVINO_MODEL, or save default_model in Settings → ASR/Whisper, "
            "or pass asr.model with an exported OpenVINO Whisper model directory."
        )

    p = Path(model_name)
    if p.exists():
        return str(p)

    candidate = model_dir / default_model_dir_name("openvino", model_name)
    if candidate.exists():
        return str(candidate)

    try:
        dest = download_model_snapshot(
            engine="openvino",
            model=model_name,
            model_dir=model_dir,
            name=default_model_dir_name("openvino", model_name),
            proxy=proxy,
        )
        return str(dest)
    except Exception as e:
        raise RuntimeError(
            f"failed to download OpenVINO Whisper model '{model_name}' into '{candidate}'. "
            "Open Settings → ASR/Whisper to download it first, or set asr.model to an existing path. "
            f"detail={type(e).__name__}: {e}"
        ) from e


def _db() -> Session:
    SessionLocal = get_sessionmaker(settings.database_url)
    return SessionLocal()


def _fresh_translate_settings() -> dict[str, Any]:
    SessionLocal = get_sessionmaker(settings.database_url)
    db = SessionLocal()
    try:
        return get_translate_settings(db, settings)
    finally:
        db.close()


def _ai_service() -> AIService:
    return AIService(_fresh_translate_settings)


def _ensure_db() -> None:
    global _DB_READY_PID
    pid = os.getpid()
    if _DB_READY_PID == pid:
        return
    with _DB_READY_LOCK:
        if _DB_READY_PID == pid:
            return
        initialize_database(settings.database_url)
        _DB_READY_PID = pid


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


_HATCHET_JOB_LEASE_TTL_SECONDS = 60


def _asr_cpu_threads(db: Session) -> int:
    del db
    concurrency = max(1, min(32, _positive_int_env("HATCHET_SUBTITLE_WORKER_SLOTS", 1)))
    model_workers = max(1, int(settings.whisper_num_workers))
    shared_budget = max(1, (process_cpu_count() or 1) // (concurrency * model_workers))
    requested = int(settings.whisper_cpu_threads)
    return min(requested, shared_budget) if requested > 0 else shared_budget


def _openvino_uses_gpu(device: str) -> bool:
    normalized = str(device or "").strip().upper()
    return normalized == "AUTO" or normalized.startswith("GPU")


def _openvino_gpu_lock_key(device: str, slot: int) -> int:
    payload = f"videoroll:openvino-gpu:{str(device or 'GPU').strip().upper()}:{int(slot)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], byteorder="big", signed=True)


@contextlib.contextmanager
def _openvino_gpu_slot(
    *,
    device: str,
    log_path: Path | None,
    cancel_check: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Serialize OpenVINO GPU model use without reducing task-worker concurrency.

    The task queue remains authoritative for Celery pool size. This gate only
    limits the scarce OpenVINO GPU resource. PostgreSQL transaction-scoped
    advisory locks coordinate prefork processes and multiple worker containers,
    and release automatically if the owning process/connection dies.
    """

    if not _openvino_uses_gpu(device):
        yield
        return

    max_slots = max(1, min(8, _positive_int_env("OPENVINO_GPU_MAX_CONCURRENCY", 1)))
    lock_db = _db()
    waiting_logged = False
    try:
        while True:
            if cancel_check is not None:
                cancel_check()
            for slot in range(max_slots):
                key = _openvino_gpu_lock_key(device, slot)
                acquired = bool(
                    lock_db.execute(
                        text("SELECT pg_try_advisory_xact_lock(:key)"),
                        {"key": key},
                    ).scalar()
                )
                if acquired:
                    note = f"asr: acquired OpenVINO GPU slot {slot + 1}/{max_slots} device={device}"
                    _safe_append_log_line(log_path, note)
                    try:
                        yield
                    finally:
                        # Transaction-scoped advisory lock is released by rollback.
                        lock_db.rollback()
                    return

            lock_db.rollback()
            if not waiting_logged:
                _safe_append_log_line(
                    log_path,
                    f"asr: waiting for OpenVINO GPU slot device={device} slots={max_slots}",
                )
                waiting_logged = True
            time.sleep(0.25)
    finally:
        try:
            lock_db.rollback()
        finally:
            lock_db.close()


def _run_asr_stage(
    *,
    db: Session,
    audio_path: Path,
    audio_key: str | None,
    asr_cfg: dict[str, Any],
    log_path: Path | None,
    groq_checkpoint_path: Path,
    cancel_check: Callable[[], None] | None = None,
) -> list[Segment]:
    """Resolve runtime ASR settings, execute one provider, and normalize its timeline."""

    asr_defaults = get_asr_settings(db, settings)
    requested_engine = str(asr_cfg.get("engine") or "auto").strip()
    requested_language = str(asr_cfg.get("language") or "auto").strip()
    requested_model = str(asr_cfg.get("model") or "").strip() or None

    engine = asr_defaults["default_engine"] if requested_engine in {"", "auto"} else requested_engine
    language = asr_defaults["default_language"] if requested_language in {"", "auto"} else requested_language
    model_name = requested_model or asr_defaults["default_model"]

    if engine == "mock":
        _safe_append_log_line(log_path, "asr: engine=mock")
        segments = transcribe_mock(audio_path)
    elif engine == "faster-whisper":
        _safe_append_log_line(log_path, f"asr: engine=faster-whisper model={model_name} language={language}")
        proxy = str(asr_defaults.get("model_download_proxy") or "").strip() or None
        model_name = _resolve_faster_whisper_model(model_name, Path(settings.whisper_model_dir), proxy=proxy)
        cpu_threads_cfg = _asr_cpu_threads(db)
        num_workers_cfg = max(1, int(getattr(settings, "whisper_num_workers", 1) or 1))
        segments = transcribe_faster_whisper(
            audio_path,
            model_name=model_name,
            language=language,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=cpu_threads_cfg,
            num_workers=num_workers_cfg,
        )
    elif engine == "openvino":
        proxy = str(asr_defaults.get("model_download_proxy") or "").strip() or None
        model_name = _resolve_openvino_model(model_name, Path(settings.whisper_model_dir), proxy=proxy)
        openvino_device = str(asr_defaults.get("openvino_device") or settings.openvino_device).strip() or settings.openvino_device
        openvino_num_beams = int(asr_defaults.get("openvino_num_beams") or settings.openvino_num_beams or 1)
        openvino_max_new_tokens = int(asr_defaults.get("openvino_max_new_tokens") or settings.openvino_max_new_tokens or 448)
        openvino_vad_enabled = bool(asr_defaults.get("openvino_vad_enabled", settings.openvino_vad_enabled))
        openvino_vad_threshold = float(asr_defaults.get("openvino_vad_threshold") or settings.openvino_vad_threshold or 0.5)
        _safe_append_log_line(
            log_path,
            "asr: "
            f"engine=openvino model={model_name} language={language} "
            f"device={openvino_device} num_beams={openvino_num_beams} max_new_tokens={openvino_max_new_tokens} "
            f"vad_enabled={openvino_vad_enabled} vad_threshold={openvino_vad_threshold}",
        )
        with _openvino_gpu_slot(
            device=openvino_device,
            log_path=log_path,
            cancel_check=cancel_check,
        ):
            segments = transcribe_openvino_whisper(
                audio_path,
                model_name=model_name,
                language=language,
                device=openvino_device,
                num_beams=openvino_num_beams,
                max_new_tokens=openvino_max_new_tokens,
                vad_enabled=openvino_vad_enabled,
                vad_threshold=openvino_vad_threshold,
            )
    elif engine == "external-whisper":
        external_base_url = str(asr_defaults.get("external_whisper_base_url") or "").strip()
        external_api_key = str(asr_defaults.get("external_whisper_api_key") or "").strip()
        external_model = str(model_name or asr_defaults.get("external_whisper_model") or "").strip()
        _safe_append_log_line(
            log_path,
            f"asr: engine=online-whisper model={external_model} base_url={external_base_url or '(empty)'} language={language}",
        )
        segments = transcribe_external_whisper(
            audio_path,
            base_url=external_base_url,
            api_key=external_api_key,
            model_name=external_model,
            language=language,
            batch_size=int(asr_defaults.get("external_whisper_batch_size") or 1),
            vad_filter=bool(asr_defaults.get("external_whisper_vad_enabled", True)),
            vad_threshold=float(asr_defaults.get("external_whisper_vad_threshold") or 0.5),
            min_silence_duration_ms=int(asr_defaults.get("external_whisper_min_silence_ms") or 500),
            speech_pad_ms=int(asr_defaults.get("external_whisper_speech_pad_ms") or 180),
            condition_on_previous_text=bool(asr_defaults.get("external_whisper_condition_on_previous_text", False)),
            max_segment_seconds=float(asr_defaults.get("external_whisper_max_segment_seconds") or 6.0),
            max_segment_chars=int(asr_defaults.get("external_whisper_max_segment_chars") or 80),
        )
    elif engine == "groq-whisper":
        groq_api_key = str(asr_defaults.get("groq_whisper_api_key") or "").strip()
        # Old rows may retain a local default_model after switching engines.
        groq_model = str(
            requested_model or asr_defaults.get("groq_whisper_model") or "whisper-large-v3-turbo"
        ).strip()
        groq_vad_enabled = bool(asr_defaults.get("openvino_vad_enabled", settings.openvino_vad_enabled))
        groq_vad_threshold = float(
            asr_defaults.get("openvino_vad_threshold") or settings.openvino_vad_threshold or 0.5
        )
        _safe_append_log_line(
            log_path,
            f"asr: engine=groq-whisper model={groq_model} language={language} "
            "format=flac chunk=45s overlap=5s retries=5 checkpoint=enabled "
            f"vad_enabled={groq_vad_enabled} vad_threshold={groq_vad_threshold}",
        )
        segments = transcribe_groq_whisper(
            audio_path,
            api_key=groq_api_key,
            model_name=groq_model,
            language=language,
            ffmpeg_path=settings.ffmpeg_path,
            checkpoint_path=groq_checkpoint_path,
            audio_identity=str(audio_key or audio_path),
            vad_enabled=groq_vad_enabled,
            vad_threshold=groq_vad_threshold,
            redis_url=str(settings.redis_url or ""),
            provider_max_concurrency=_positive_int_env("GROQ_ASR_MAX_CONCURRENCY", 1),
        )
    elif engine == "cloudflare-workers-ai":
        cloudflare_account_id = str(asr_defaults.get("cloudflare_workers_ai_account_id") or "").strip()
        cloudflare_api_key = str(asr_defaults.get("cloudflare_workers_ai_api_key") or "").strip()
        cloudflare_model = str(model_name or asr_defaults.get("cloudflare_workers_ai_model") or "").strip()
        _safe_append_log_line(
            log_path,
            f"asr: engine=cloudflare-workers-ai model={cloudflare_model} "
            f"account_id={cloudflare_account_id or '(empty)'} language={language}",
        )
        segments = transcribe_cloudflare_workers_ai(
            audio_path,
            account_id=cloudflare_account_id,
            api_key=cloudflare_api_key,
            model_name=cloudflare_model,
            language=language,
            redis_url=str(settings.redis_url or ""),
            provider_max_concurrency=_positive_int_env("CLOUDFLARE_ASR_MAX_CONCURRENCY", 2),
        )
    else:
        raise ValueError(f"unsupported ASR engine: {engine}")

    raw_segments = sorted(segments, key=lambda item: (item.start, item.end, item.text))
    overlap_count = sum(
        1
        for previous, current in zip(raw_segments, raw_segments[1:])
        if current.start < previous.end
    )
    reconciled = reconcile_overlapping_asr_segments(raw_segments)
    if overlap_count or len(reconciled) != len(raw_segments):
        _safe_append_log_line(
            log_path,
            "asr timeline reconciled before translation: "
            f"overlaps={overlap_count} segments={len(raw_segments)}->{len(reconciled)}",
        )
    return reconciled


class _TaskStopped(Exception):
    """Raised at a safe boundary when a user has stopped a task."""


def _task_is_stopped(db: Session, task_id: uuid.UUID) -> bool:
    return db.query(Task.status).filter(Task.id == task_id).scalar() == TaskStatus.canceled


def _raise_if_task_stopped(db: Session, task_id: uuid.UUID) -> None:
    if _task_is_stopped(db, task_id):
        raise _TaskStopped("task stopped by user")


def _pause_subtitle_job_if_task_stopped(db: Session, job_id: uuid.UUID) -> bool:
    job = db.get(SubtitleJob, job_id)
    if not job:
        return False
    if not _task_is_stopped(db, job.task_id):
        return False
    task = db.get(Task, job.task_id)
    if not task:
        return False
    db.refresh(task)
    if job.status == SubtitleJobStatus.running:
        request = dict(job.request_json) if isinstance(job.request_json, dict) else {}
        request["resume"] = True
        job.request_json = request
        job.status = SubtitleJobStatus.queued
        job.progress = 0
        job.error_message = _task_queue_join_message(job.error_message, "Task stopped by user; waiting for resume.")
        db.add(job)
    db.commit()
    return True


def _task_queue_join_message(message: str | None, detail: str, *, limit: int = 2000) -> str:
    head = str(message or "").strip()
    tail = str(detail or "").strip()
    if tail and tail in {line.strip() for line in head.splitlines()}:
        return head[-limit:]
    if head and tail:
        out = f"{head}\n{tail}"
    else:
        out = head or tail
    if len(out) > limit:
        out = out[: limit - 1] + "…"
    return out


def _append_log_line(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    msg = (message or "").rstrip("\n")
    ts = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("ab") as f:
        f.write(f"[{ts}] {msg}\n".encode("utf-8", errors="replace"))


def _append_log_block(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    data = (text or "").rstrip("\n") + "\n"
    with log_path.open("ab") as f:
        f.write(data.encode("utf-8", errors="replace"))


def _safe_append_log_line(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    try:
        _append_log_line(log_path, message)
    except Exception:
        pass


def _safe_append_log_block(log_path: Path | None, text: str) -> None:
    if log_path is None:
        return
    try:
        _append_log_block(log_path, text)
    except Exception:
        pass


def _safe_upload_log(store: FileStore, log_path: Path | None, log_key: str | None) -> None:
    if log_path is None or not log_key:
        return
    try:
        if log_path.exists():
            store.upload_file(log_path, log_key, content_type="text/plain")
            key_parts = log_key.split("/", 2)
            if len(key_parts) >= 3 and key_parts[0] == "log" and key_parts[1]:
                publish_log_updated(settings.redis_url, task_id=key_parts[1], storage_key=log_key)
    except Exception:
        pass


def _cleanup_local_work_root(path: Path | None) -> None:
    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _seed_log_from_store(store: FileStore, log_key: str, log_path: Path) -> None:
    try:
        if log_path.exists() and log_path.stat().st_size > 0:
            return
    except Exception:
        return
    try:
        store.download_file(log_key, log_path)
    except Exception:
        pass


def _ensure_log_asset(db: Session, task_id: uuid.UUID, log_key: str) -> None:
    existing = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.log, Asset.storage_key == log_key)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if existing:
        return
    db.add(Asset(task_id=task_id, kind=AssetKind.log, storage_key=log_key))


def _effective_subtitle_worker_youtube_settings(db: Session, *, cookie_dir: Path | None = None) -> Any:
    cfg = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    proxy = str(cfg.get("proxy") or "").strip()
    cookies_enabled = bool(cfg.get("cookies_enabled"))

    cookie_file = str(getattr(settings, "youtube_cookie_file", "") or "").strip() or None
    if cookie_file:
        try:
            if not Path(cookie_file).is_file() and cookie_dir is not None and cookies_enabled:
                cookie_file = None
        except Exception:
            pass

    if not cookie_file and cookie_dir is not None and cookies_enabled:
        cookies_txt = get_youtube_cookies_txt(db)
        if cookies_txt:
            cookies_txt = normalize_and_validate_netscape_cookies_txt(cookies_txt)
            try:
                cookie_dir.mkdir(parents=True, exist_ok=True)
                cookie_path = cookie_dir / "youtube_cookies.txt"
                cookie_path.write_text(cookies_txt, encoding="utf-8")
                try:
                    os.chmod(cookie_path, 0o600)
                except Exception:
                    pass
                cookie_file = str(cookie_path)
            except Exception:
                cookie_file = None

    return settings.model_copy(
        update={
            "youtube_proxy": proxy or None,
            "youtube_cookie_file": cookie_file,
            "youtube_compatibility_mode_enabled": bool(cfg.get("compatibility_mode_enabled")),
        }
    )


def _download_youtube_subtitle_segments(
    *,
    task: Task,
    db: Session,
    work_root: Path,
    log_path: Path | None,
    log_key: str | None,
    store: FileStore,
    target_lang: str,
    youtube_subtitle_mode: str,
) -> tuple[list[Segment] | None, dict[str, str] | None]:
    normalized_mode = normalize_youtube_subtitle_mode(youtube_subtitle_mode)
    if task.source_type != SourceType.youtube or normalized_mode == "off":
        return None, None

    source_url = str(task.source_url or "").strip()
    if not source_url:
        return None, None

    _safe_append_log_line(
        log_path,
        f"youtube subtitles: probing mode={normalized_mode} target_lang={target_lang or 'zh'}",
    )
    _safe_upload_log(store, log_path, log_key)

    try:
        with tempfile.TemporaryDirectory(prefix="ytsub_", dir=str(work_root)) as tmp:
            tmp_dir = Path(tmp)
            yt_settings = _effective_subtitle_worker_youtube_settings(db, cookie_dir=tmp_dir)
            started_at = time.monotonic()
            _safe_append_log_line(log_path, "youtube subtitles: fetching metadata via yt-dlp")
            _safe_upload_log(store, log_path, log_key)
            info, _meta = extract_youtube_metadata(
                source_url,
                yt_settings,
                extractor_args_override={"youtube": {"skip": ["translated_subs"]}},
            )
            _safe_append_log_line(
                log_path,
                f"youtube subtitles: metadata fetched in {time.monotonic() - started_at:.1f}s",
            )
            _safe_upload_log(store, log_path, log_key)
            selection = pick_preferred_youtube_subtitle(
                info,
                target_lang=target_lang,
                mode=normalized_mode,
            )
            if selection is None:
                if normalized_mode == "auto_source":
                    _safe_append_log_line(
                        log_path,
                        "youtube subtitles: no auto-generated source subtitles found; fallback to ASR",
                    )
                else:
                    _safe_append_log_line(
                        log_path,
                        "youtube subtitles: no target-language subtitles found; fallback to ASR",
                    )
                return None, None

            _safe_append_log_line(
                log_path,
                f"youtube subtitles: downloading track source={selection.source} "
                f"language={selection.language} reason={selection.reason}",
            )
            _safe_upload_log(store, log_path, log_key)
            subtitle_path, _subtitle_info, _subtitle_meta = download_youtube_subtitle(
                source_url,
                yt_settings,
                work_dir=tmp_dir,
                selection=selection,
            )
            srt_input_path = subtitle_path
            if subtitle_path.suffix.lower() != ".srt":
                srt_input_path = tmp_dir / f"{subtitle_path.stem}.srt"
                _safe_append_log_line(
                    log_path,
                    f"youtube subtitles: converting {subtitle_path.suffix.lower() or '(unknown)'} -> srt",
                )
                convert_subtitle_to_srt(
                    settings.ffmpeg_path,
                    subtitle_path,
                    srt_input_path,
                    log_path=log_path,
                )

            segments = srt_to_segments(srt_input_path.read_text(encoding="utf-8"))
            if not segments:
                _safe_append_log_line(
                    log_path,
                    "youtube subtitles: parsed 0 segments; fallback to ASR",
                )
                return None, None

            _safe_append_log_line(
                log_path,
                f"youtube subtitles: selected {selection.source}:{selection.language} "
                f"reason={selection.reason} segments={len(segments)}",
            )
            return segments, {
                "language": selection.language,
                "source": selection.source,
                "reason": selection.reason,
            }
    except Exception as error:
        _safe_append_log_line(
            log_path,
            f"youtube subtitles: probe/download failed; fallback to ASR: "
            f"{type(error).__name__}: {error}",
        )
        _safe_upload_log(store, log_path, log_key)
        return None, None


@worker_init.connect
def _on_worker_init(**_kwargs: Any) -> None:
    """Initialize runtime state for the remaining Celery control worker."""
    # Subtitle execution is owned by Hatchet. Celery remains temporarily for
    # outbox/publish control tasks only.
    _orchestrator_internal_headers()
    try:
        _ensure_db()
    except Exception:
        logger.exception("subtitle control worker initialization failed")


class SubtitleJobExecutionFailed(RuntimeError):
    """Terminal subtitle execution failure surfaced to non-Celery executors."""


class SubtitleJobLeaseBusy(RuntimeError):
    """Retryable conflict while another executor still owns a subtitle job lease."""


def run_subtitle_job(
    job_id: str,
    *,
    retry_attempt: int = 0,
    raise_on_error: bool = False,
) -> dict[str, str]:
    _ensure_db()
    store = FileStore(settings)
    store.ensure_ready()

    jid = uuid.UUID(job_id)
    db = _db()
    log_path: Path | None = None
    log_key: str | None = None
    job_hb: JobLeaseHeartbeat | None = None
    lease_owner: str | None = None
    work_root: Path | None = None
    ai_usage_tokens: tuple[Any, Any] | None = None
    try:
        job = db.get(SubtitleJob, jid)
        if not job:
            return {"status": "error", "detail": "job not found"}

        if job.status == SubtitleJobStatus.succeeded:
            return {"status": "ok", "detail": "job already succeeded"}
        if job.status == SubtitleJobStatus.failed:
            return {"status": "skipped", "detail": "job already failed"}

        task = db.get(Task, job.task_id)
        if not task:
            job.status = SubtitleJobStatus.failed
            job.error_message = "task not found"
            db.add(job)
            db.commit()
            return {"status": "error", "detail": "task not found"}
        ai_usage_tokens = set_ai_usage_context(task_id=task.id, operation="subtitle")
        if task.status == TaskStatus.published:
            job.status = SubtitleJobStatus.failed
            job.error_message = _task_queue_join_message(
                job.error_message,
                "Task is already published; stale subtitle job skipped.",
            )
            db.add(job)
            db.commit()
            return {"status": "skipped", "detail": "task already published"}
        if _pause_subtitle_job_if_task_stopped(db, jid):
            return {"status": "stopped", "detail": "task stopped by user"}

        now = _now()
        if job.status == SubtitleJobStatus.running:
            lease_until = job.lease_until
            if lease_until is not None and lease_until.tzinfo is None:
                lease_until = lease_until.replace(tzinfo=timezone.utc)
            if lease_until is not None and lease_until > now:
                raise SubtitleJobLeaseBusy(f"subtitle job {job.id} is still leased by {job.lease_owner or 'another worker'}")
            request = dict(job.request_json) if isinstance(job.request_json, dict) else {}
            request["resume"] = True
            job.request_json = request
            job.status = SubtitleJobStatus.queued
            job.progress = 0
            job.lease_owner = None
            job.lease_until = None
            job.heartbeat_at = now
            db.add(job)
            db.flush()
        db.refresh(job)
        db.refresh(task)
        if job.status != SubtitleJobStatus.queued:
            return {"status": "in_progress", "detail": "job was already claimed or completed"}
        # Hatchet owns scheduling; the database lease protects against duplicate
        # delivery or overlapping retries of the same SubtitleJob.
        job.status = SubtitleJobStatus.running
        job.progress = max(int(job.progress or 0), 2)
        db.add(job)
        db.flush()
        lease_ttl_seconds = _HATCHET_JOB_LEASE_TTL_SECONDS
        candidate_owner = f"hatchet.subtitle_job:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        if not acquire_job_lease(db, job, candidate_owner, lease_ttl_seconds):
            db.rollback()
            raise SubtitleJobLeaseBusy(f"subtitle job {job.id} lease claim lost to another worker")
        db.commit()
        lease_owner = candidate_owner

        job_hb = JobLeaseHeartbeat(lambda: _db(), job.id, lease_owner, lease_ttl_seconds)
        job_hb.start()

        _raise_if_task_stopped(db, task.id)

        req = dict(job.request_json) if isinstance(job.request_json, dict) else {}
        input_key = (req.get("input") or {}).get("key")
        if not input_key:
            raise ValueError("missing input.key")

        work_root = Path(settings.work_dir) / "subtitle" / str(job.id)
        work_root.mkdir(parents=True, exist_ok=True)

        video_path = store.path_for(input_key)
        audio_path = work_root / "audio.wav"
        segments_path = work_root / "segments.json"
        subtitle_segments_path = work_root / "subtitle_segments.json"
        translation_checkpoint_path = work_root / "translation_checkpoint.json"
        srt_path = work_root / "subtitle_zh.srt"
        ass_path = work_root / "subtitle_zh.ass"

        audio_key: str | None = None
        segments_key: str | None = None
        srt_key: str | None = None
        ass_key: str | None = None

        resume = bool(req.get("resume"))
        auto_task_meta = parse_auto_youtube_created_by(task.created_by)
        automatic_runtime_profile = _uses_runtime_auto_profile(task, req)

        def _current_auto_profile() -> dict[str, Any]:
            return dict(get_auto_profile(db)) if automatic_runtime_profile else {}

        initial_profile = _current_auto_profile()
        output_cfg = (req.get("output") or {})
        formats = (
            list(initial_profile.get("formats") or ["srt", "ass"])
            if automatic_runtime_profile
            else list(output_cfg.get("formats") or [])
        )
        render_cfg = dict(output_cfg.get("render") or {})
        if automatic_runtime_profile:
            render_cfg = {
                "burn_in": bool(initial_profile.get("burn_in")),
                "soft_sub": bool(initial_profile.get("soft_sub")),
                "ass_style": initial_profile.get("ass_style") or "clean_white",
                "video_codec": initial_profile.get("video_codec") or "av1",
                "use_intel_gpu": bool(initial_profile.get("use_intel_gpu")),
                "video_preset": initial_profile.get("video_preset"),
                "video_crf": initial_profile.get("video_crf"),
                "primary_font_scale_percent": initial_profile.get("primary_font_scale_percent") or 100,
                "secondary_font_scale_percent": initial_profile.get("secondary_font_scale_percent") or 100,
            }
        burn_in = bool(render_cfg.get("burn_in"))
        soft_sub = bool(render_cfg.get("soft_sub"))
        video_codec = str(render_cfg.get("video_codec") or "av1").strip().lower() or "av1"
        use_intel_gpu = bool(render_cfg.get("use_intel_gpu"))
        video_crf = render_cfg.get("video_crf")
        video_preset = render_cfg.get("video_preset")

        want_ass = "ass" in formats
        # Automatic burn-in regenerates ASS inside the render box using the
        # style/font settings current when rendering actually starts.
        need_ass = want_ass or (burn_in and not automatic_runtime_profile)
        if automatic_runtime_profile:
            youtube_subtitle_mode = normalize_youtube_subtitle_mode(
                initial_profile.get("youtube_subtitle_mode"),
                prefer_youtube_subtitles=initial_profile.get("prefer_youtube_subtitles", True),
            )
            translate_cfg = {
                "enabled": bool(initial_profile.get("translate_enabled")),
                "target_lang": initial_profile.get("target_lang") or "zh",
                "provider": initial_profile.get("translate_provider") or "openai",
                "style": initial_profile.get("translate_style") or "口语自然",
                "enable_summary": bool(initial_profile.get("translate_enable_summary")),
                "bilingual": bool(initial_profile.get("bilingual")),
            }
        else:
            youtube_subtitle_mode = normalize_youtube_subtitle_mode(
                req.get("youtube_subtitle_mode"),
                prefer_youtube_subtitles=req.get("prefer_youtube_subtitles", True),
            )
            translate_cfg = dict(req.get("translate") or {})
        prefer_youtube_subtitles = youtube_subtitle_mode != "off"
        translate_enabled = bool(translate_cfg.get("enabled"))
        target_lang = str(translate_cfg.get("target_lang") or "zh").strip() or "zh"
        provider = str(translate_cfg.get("provider") or "mock").strip() or "mock"
        bilingual = bool(translate_cfg.get("bilingual"))

        log_path = work_root / "job.log"
        log_key = f"log/{task.id}/subtitle_{job.id}.log"
        job.logs_key = log_key
        db.add(job)
        db.commit()
        try:
            _ensure_log_asset(db, task.id, log_key)
            db.commit()
        except Exception:
            db.rollback()

        _seed_log_from_store(store, log_key, log_path)
        _safe_append_log_line(
            log_path,
            f"subtitle job start: job_id={job.id} task_id={task.id} resume={resume} formats={formats} burn_in={burn_in} soft_sub={soft_sub} intel_gpu={use_intel_gpu}",
        )
        _safe_upload_log(store, log_path, log_key)

        def _download_latest_asset(kind: AssetKind, dest: Path, *, direct: bool = False) -> Asset | None:
            row = (
                db.query(Asset)
                .filter(Asset.task_id == task.id, Asset.kind == kind)
                .order_by(Asset.created_at.desc())
                .first()
            )
            if not row:
                return None
            try:
                if direct:
                    source = store.path_for(row.storage_key)
                    return row if source.stat().st_size > 0 else None
                store.download_file(row.storage_key, dest)
                if dest.exists() and dest.stat().st_size > 0:
                    return row
            except Exception:
                return None
            return None

        def _save_job_request() -> None:
            nonlocal req
            job.request_json = req
            db.add(job)

        def _final_subtitle_segments_key() -> str | None:
            artifacts = req.get("artifacts")
            if not isinstance(artifacts, dict):
                return None
            key = str(artifacts.get("final_subtitle_segments_key") or "").strip()
            return key or None

        def _set_final_subtitle_segments_key(key: str) -> None:
            artifacts = dict(req.get("artifacts") or {})
            artifacts["final_subtitle_segments_key"] = key
            req["artifacts"] = artifacts
            _save_job_request()

        def _set_youtube_subtitle_info(info: dict[str, str]) -> None:
            artifacts = dict(req.get("artifacts") or {})
            artifacts["youtube_subtitle"] = dict(info)
            req["artifacts"] = artifacts
            _save_job_request()

        def _load_segments_json(path: Path) -> list[Segment] | None:
            try:
                return segments_from_json_data(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                return None

        def _download_final_subtitle_segments() -> list[Segment] | None:
            key = _final_subtitle_segments_key()
            if not key:
                return None
            try:
                store.download_file(key, subtitle_segments_path)
            except Exception:
                return None
            segs = _load_segments_json(subtitle_segments_path)
            return segs or None

        def _ensure_video() -> None:
            if not video_path.is_file():
                raise FileNotFoundError(video_path)

        def _store_source_segments(segs: list[Segment], *, source_label: str) -> None:
            nonlocal segments_key
            write_json(segments_path, segments_to_json_data(segs))
            segments_sha = sha256_file(segments_path)
            segments_key = _unique_storage_key(
                f"sub/{task.id}/segments",
                segments_sha,
                ".json",
            )
            store.upload_file(segments_path, segments_key, content_type="application/json")
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.segments_json,
                    storage_key=segments_key,
                    sha256=segments_sha,
                    size_bytes=segments_path.stat().st_size,
                )
            )
            _raise_if_task_stopped(db, task.id)
            task.status = TaskStatus.asr_done
            db.add(task)
            db.commit()
            _clear_groq_asr_checkpoint()
            _safe_append_log_line(log_path, f"{source_label}: segments={len(segs)}")
            _safe_upload_log(store, log_path, log_key)

        def _groq_asr_checkpoint_key() -> str:
            return f"sub/{task.id}/groq_asr_checkpoint.json"

        def _groq_asr_checkpoint_path() -> Path:
            return store.path_for(_groq_asr_checkpoint_key(), require_exists=False)

        def _clear_groq_asr_checkpoint() -> None:
            try:
                store.delete_object(_groq_asr_checkpoint_key())
            except Exception:
                pass

        translation_checkpoint = TranslationCheckpointStore(
            store=store,
            task_id=task.id,
            local_path=translation_checkpoint_path,
            log=lambda message: _safe_append_log_line(log_path, message),
        )

        if not resume:
            translation_checkpoint.clear()
            _clear_groq_asr_checkpoint()

        srt_asset = _download_latest_asset(AssetKind.subtitle_srt, srt_path) if resume else None
        if resume and srt_asset:
            srt_key = srt_asset.storage_key
            translation_checkpoint.clear()
            _clear_groq_asr_checkpoint()
            _safe_append_log_line(log_path, f"resume: found existing subtitle_srt asset: {srt_key}")
            _safe_upload_log(store, log_path, log_key)

            mark_subtitle_ready(
                db=db,
                task=task,
                job=job,
                resume_existing=True,
            )

            if automatic_runtime_profile:
                output_profile = _current_auto_profile()
                need_ass = "ass" in list(output_profile.get("formats") or ["srt", "ass"])
            if need_ass:
                segs = _download_final_subtitle_segments()
                ass_bilingual = bool(
                    (req.get("translate") if isinstance(req.get("translate"), dict) else {}).get("bilingual")
                )
                if segs:
                    ass_key = store_ass_output(
                        db=db,
                        store=store,
                        task_id=task.id,
                        segments=segs,
                        ass_path=ass_path,
                        video_path=video_path,
                        ffmpeg_path=settings.ffmpeg_path,
                        render_cfg=render_cfg,
                        bilingual=ass_bilingual,
                        unique_storage_key=_unique_storage_key,
                        log=lambda message: _safe_append_log_line(log_path, message),
                        log_prefix="generated ass from resumed subtitle segments",
                    )
                else:
                    segs = srt_to_segments(srt_path.read_text(encoding="utf-8"))
                    ass_key = store_ass_output(
                        db=db,
                        store=store,
                        task_id=task.id,
                        segments=segs,
                        ass_path=ass_path,
                        video_path=video_path,
                        ffmpeg_path=settings.ffmpeg_path,
                        render_cfg=render_cfg,
                        bilingual=ass_bilingual,
                        unique_storage_key=_unique_storage_key,
                        log=lambda message: _safe_append_log_line(log_path, message),
                        log_prefix="generated ass from resumed srt (fallback)",
                    )
                    _safe_append_log_line(
                        log_path,
                        "resume: final subtitle segments not found; ass regenerated from srt without structured bilingual data",
                    )
                db.commit()
                _safe_upload_log(store, log_path, log_key)

            render_payload = build_render_job_payload(
                request_json=req,
                input_key=input_key,
                srt_key=srt_key,
                ass_key=ass_key,
                automatic_runtime_profile=automatic_runtime_profile,
                burn_in=burn_in,
                soft_sub=soft_sub,
                video_codec=video_codec,
                use_intel_gpu=use_intel_gpu,
                video_preset=video_preset,
                video_crf=video_crf,
            )
            return complete_subtitle_handoff(
                db=db,
                task=task,
                job=job,
                automatic_runtime_profile=automatic_runtime_profile,
                burn_in=burn_in,
                soft_sub=soft_sub,
                render_payload=render_payload,
                ensure_not_stopped=lambda: _raise_if_task_stopped(db, task.id),
                log=lambda message: _safe_append_log_line(log_path, message),
                upload_log=lambda: _safe_upload_log(store, log_path, log_key),
            )

        segments: list[Segment] | None = None
        segments_asset = _download_latest_asset(AssetKind.segments_json, segments_path) if resume else None
        if resume and segments_asset:
            segments_key = segments_asset.storage_key
            segments = _load_segments_json(segments_path)
            if segments is not None:
                _clear_groq_asr_checkpoint()

        youtube_subtitle_info: dict[str, str] | None = None
        request_artifacts = req.get("artifacts") if isinstance(req.get("artifacts"), dict) else {}
        existing_youtube_subtitle = (
            request_artifacts.get("youtube_subtitle")
            if isinstance(request_artifacts.get("youtube_subtitle"), dict)
            else {}
        )
        skip_translation_for_target_subtitle = existing_youtube_subtitle.get("reason") == "target"
        if segments is None:
            job.progress = 25
            db.add(job)
            db.commit()

            if automatic_runtime_profile:
                subtitle_source_profile = _current_auto_profile()
                youtube_subtitle_mode = normalize_youtube_subtitle_mode(
                    subtitle_source_profile.get("youtube_subtitle_mode"),
                    prefer_youtube_subtitles=subtitle_source_profile.get("prefer_youtube_subtitles", True),
                )
                prefer_youtube_subtitles = youtube_subtitle_mode != "off"
                target_lang = str(subtitle_source_profile.get("target_lang") or "zh").strip() or "zh"

            if prefer_youtube_subtitles:
                segments, youtube_subtitle_info = _download_youtube_subtitle_segments(
                    task=task,
                    db=db,
                    work_root=work_root,
                    log_path=log_path,
                    log_key=log_key,
                    store=store,
                    target_lang=target_lang,
                    youtube_subtitle_mode=youtube_subtitle_mode,
                )
                if segments:
                    if youtube_subtitle_info:
                        _set_youtube_subtitle_info(youtube_subtitle_info)
                    if youtube_subtitle_info and youtube_subtitle_info.get("reason") == "target":
                        skip_translation_for_target_subtitle = True
                        if not automatic_runtime_profile:
                            translate_cfg = dict(req.get("translate") or {})
                            translate_cfg["enabled"] = False
                            req["translate"] = translate_cfg
                        translate_enabled = False
                        _safe_append_log_line(log_path, "youtube subtitles: target language subtitle found; skipping translation")
                    elif youtube_subtitle_info and youtube_subtitle_info.get("reason") == "auto_source":
                        if translate_enabled:
                            _safe_append_log_line(log_path, "youtube subtitles: auto-generated source subtitle found; translation will run instead of ASR")
                        else:
                            _safe_append_log_line(log_path, "youtube subtitles: auto-generated source subtitle found; translation disabled; using it directly")
                    _save_job_request()
                    db.commit()
                    _store_source_segments(segments, source_label="youtube subtitles ready")

        if segments is None:
            audio_asset = _download_latest_asset(AssetKind.audio_wav, audio_path, direct=True) if resume else None
            if resume and audio_asset:
                audio_key = audio_asset.storage_key
                audio_path = store.path_for(audio_key)
            else:
                _ensure_video()
                _safe_append_log_line(log_path, "ffmpeg: extract audio")
                extract_audio(settings.ffmpeg_path, video_path, audio_path, log_path=log_path)
                _safe_upload_log(store, log_path, log_key)
                audio_sha = sha256_file(audio_path)
                audio_key = _unique_storage_key(
                    f"work/{task.id}/audio",
                    audio_sha,
                    ".wav",
                )
                audio_size = audio_path.stat().st_size
                store.promote_file(audio_path, audio_key)
                audio_path = store.path_for(audio_key)
                db.add(
                    Asset(
                        task_id=task.id,
                        kind=AssetKind.audio_wav,
                        storage_key=audio_key,
                        sha256=audio_sha,
                        size_bytes=audio_size,
                    )
                )
            _raise_if_task_stopped(db, task.id)
            task.status = TaskStatus.audio_extracted
            db.add(task)
            db.commit()

            # ASR is a runtime-configured box for automatic tasks.  Read the
            # auto profile only now, immediately before ASR starts; manual
            # requests retain their explicit per-job options.
            if automatic_runtime_profile:
                asr_profile = _current_auto_profile()
                asr_cfg = {
                    "engine": asr_profile.get("asr_engine") or "auto",
                    "language": asr_profile.get("asr_language") or "auto",
                    "model": asr_profile.get("asr_model"),
                }
            else:
                asr_cfg = dict(req.get("asr") or {})
            segments = _run_asr_stage(
                db=db,
                audio_path=audio_path,
                audio_key=audio_key,
                asr_cfg=asr_cfg,
                log_path=log_path,
                groq_checkpoint_path=_groq_asr_checkpoint_path(),
                cancel_check=lambda: _raise_if_task_stopped(db, task.id),
            )
            _store_source_segments(segments, source_label="asr done")

        job.progress = 60
        db.add(job)
        db.commit()

        # Translation/RAG is a separate box: automatic backlog reads the
        # latest auto profile and detailed translation settings only now.
        if automatic_runtime_profile:
            translate_profile = _current_auto_profile()
            translate_cfg = {
                "enabled": bool(translate_profile.get("translate_enabled")),
                "target_lang": translate_profile.get("target_lang") or "zh",
                "provider": translate_profile.get("translate_provider") or "openai",
                "style": translate_profile.get("translate_style") or "口语自然",
                "enable_summary": bool(translate_profile.get("translate_enable_summary")),
                "bilingual": bool(translate_profile.get("bilingual")),
            }
        else:
            translate_cfg = dict(req.get("translate") or {})
        if skip_translation_for_target_subtitle:
            translate_cfg["enabled"] = False

        try:
            translation_result = run_translation_stage(
                db=db,
                task_id=str(task.id),
                subtitle_job_id=str(job.id),
                segments=segments,
                source_segments_key=segments_key,
                translate_cfg=translate_cfg,
                checkpoint=translation_checkpoint,
                trace=translation_trace_recorder(db),
                retry_attempt=max(0, int(retry_attempt or 0)),
                database_url=settings.database_url,
                fresh_translate_settings=_fresh_translate_settings,
                ai_service_factory=_ai_service,
                log=lambda message: _safe_append_log_line(log_path, message),
            )
        except TranslationRetryRequired as retry:
            req["resume"] = True
            _save_job_request()
            job.error_message = (
                f"translate failed; retrying "
                f"({retry.retry_no}/{retry.max_retries}): {retry.cause}"
            )
            db.add(job)
            db.commit()
            _safe_append_log_line(
                log_path,
                f"translate retry {retry.retry_no}/{retry.max_retries}: "
                f"{type(retry.cause).__name__}: {retry.cause}",
            )
            _safe_upload_log(store, log_path, log_key)
            raise

        translate_enabled = translation_result.enabled
        provider = translation_result.provider
        target_lang = translation_result.target_lang
        bilingual = translation_result.bilingual
        segments_out = translation_result.segments
        translation_summary = translation_result.summary
        if translate_enabled:
            style = translation_result.style
            ai_service = translation_result.ai_service
            if ai_service is None:
                raise RuntimeError("translation stage returned no AI service")
            job.error_message = None
            db.add(job)
            db.commit()
            _raise_if_task_stopped(db, task.id)
            task.status = TaskStatus.translated
            db.add(task)

            segments_out = postprocess_translation(
                db=db,
                store=store,
                task=task,
                request_json=req,
                source_segments=segments,
                translated_segments=segments_out,
                provider=provider,
                target_lang=target_lang,
                style=style,
                summary=translation_summary,
                bilingual=bilingual,
                ai_service=ai_service,
                log=lambda message: _safe_append_log_line(log_path, message),
            )

        if automatic_runtime_profile:
            output_profile = _current_auto_profile()
            need_ass = "ass" in list(output_profile.get("formats") or ["srt", "ass"])
        ass_bilingual = bool(
            (req.get("translate") if isinstance(req.get("translate"), dict) else {}).get("bilingual")
        )
        outputs = persist_subtitle_outputs(
            db=db,
            store=store,
            task=task,
            job=job,
            request_json=req,
            segments=segments_out,
            subtitle_segments_path=subtitle_segments_path,
            srt_path=srt_path,
            ass_path=ass_path,
            video_path=video_path,
            ffmpeg_path=settings.ffmpeg_path,
            render_cfg=render_cfg,
            need_ass=need_ass,
            ass_bilingual=ass_bilingual,
            unique_storage_key=_unique_storage_key,
            clear_translation_checkpoint=translation_checkpoint.clear,
            log=lambda message: _safe_append_log_line(log_path, message),
        )
        srt_key = outputs.srt_key
        ass_key = outputs.ass_key

        _raise_if_task_stopped(db, task.id)
        mark_subtitle_ready(
            db=db,
            task=task,
            job=job,
            resume_existing=False,
        )
        _safe_upload_log(store, log_path, log_key)

        render_payload = build_render_job_payload(
            request_json=req,
            input_key=input_key,
            srt_key=srt_key,
            ass_key=ass_key,
            automatic_runtime_profile=automatic_runtime_profile,
            burn_in=burn_in,
            soft_sub=soft_sub,
            video_codec=video_codec,
            use_intel_gpu=use_intel_gpu,
            video_preset=video_preset,
            video_crf=video_crf,
        )
        return complete_subtitle_handoff(
            db=db,
            task=task,
            job=job,
            automatic_runtime_profile=automatic_runtime_profile,
            burn_in=burn_in,
            soft_sub=soft_sub,
            render_payload=render_payload,
            ensure_not_stopped=lambda: _raise_if_task_stopped(db, task.id),
            log=lambda message: _safe_append_log_line(log_path, message),
            upload_log=lambda: _safe_upload_log(store, log_path, log_key),
        )
    except _TaskStopped:
        _pause_subtitle_job_if_task_stopped(db, jid)
        return {"status": "stopped", "detail": "task stopped by user"}
    except SubtitleJobLeaseBusy:
        raise
    except TranslationRetryRequired:
        retry_job = db.get(SubtitleJob, jid)
        if retry_job and retry_job.status == SubtitleJobStatus.running:
            retry_job.status = SubtitleJobStatus.queued
            db.add(retry_job)
            db.commit()
        raise
    except Exception as e:
        job = db.get(SubtitleJob, uuid.UUID(job_id))
        if job:
            job.status = SubtitleJobStatus.failed
            job.error_message = str(e)
            db.add(job)
            task = db.get(Task, job.task_id)
            if task and not _task_is_stopped(db, task.id) and task.status != TaskStatus.published:
                task.status = TaskStatus.failed
                task.error_code = task.error_code or "SUBTITLE_FAILED"
                task.error_message = str(e)
                db.add(task)
        db.commit()
        _safe_append_log_line(log_path, f"ERROR: {type(e).__name__}: {e}")
        _safe_append_log_block(log_path, traceback.format_exc())
        _safe_upload_log(store, log_path, log_key)
        if raise_on_error:
            raise SubtitleJobExecutionFailed(str(e)) from e
        return {"status": "error", "detail": str(e)}
    finally:
        if job_hb is not None:
            job_hb.stop()
        if lease_owner is not None:
            lease_db = _db()
            try:
                release_job_lease(lease_db, jid, lease_owner)
                lease_db.commit()
            except Exception:
                lease_db.rollback()
                logger.exception("failed to release subtitle job lease (job_id=%s)", jid)
            finally:
                lease_db.close()
        reset_ai_usage_context(ai_usage_tokens)
        db.close()
        _cleanup_local_work_root(work_root)


def _after_render_publish_impl(render_job_id: str) -> dict[str, Any]:
    _ensure_db()
    db = _db()
    try:
        rid = uuid.UUID(render_job_id)
        rj = db.get(RenderJob, rid)
        if not rj:
            return {"status": "error", "detail": "render job not found"}

        task = db.get(Task, rj.task_id)
        if not task:
            return {"status": "error", "detail": "task not found"}
        req = rj.request_json if isinstance(rj.request_json, dict) else {}
        after_render = req.get("after_render") if isinstance(req.get("after_render"), dict) else {}
        if not after_render.get("publish"):
            return {"status": "skipped"}
        publish_payload = after_render.get("publish_payload") or after_render.get("payload") or {}
        if not isinstance(publish_payload, dict):
            return {"status": "error", "detail": "after_render.publish_payload must be an object"}
        publish_payload = dict(publish_payload)
        store = FileStore(settings)
        store.ensure_ready()

        from videoroll.apps.orchestrator_api.schemas import PublishAllRequest
        from videoroll.apps.orchestrator_api.services.publishing_service import publish_all

        # Let publish_all resolve the latest rendered asset unless a manual
        # request explicitly pinned one.
        if publish_payload.get("video_key") in {"", None}:
            publish_payload["video_key"] = None

        result_data = publish_all(
            task.id,
            PublishAllRequest.model_validate(publish_payload),
            get_orchestrator_settings(),
            db,
            store,
        )

        # Log partial failures but don't fail the task if at least one platform succeeded.
        errors = result_data.get("errors", {}) if isinstance(result_data, dict) else {}
        if errors:
            logger.warning("after_render_publish partial failure for task %s: %s", task.id, errors)
        if not result_data.get("has_any_accepted", False) and errors:
            error_details = "; ".join(f"{p}: {msg}" for p, msg in errors.items())
            return {"status": "error", "detail": f"all platforms failed: {error_details}", "platforms": result_data}

        return {"status": "ok", "platforms": result_data}
    except Exception as e:
        task = db.get(Task, rj.task_id) if "rj" in locals() and rj else None
        if task:
            if task.status == TaskStatus.ready_for_review and task.error_code == "AI_REVIEW_REJECTED":
                return {"status": "review_rejected", "detail": task.error_message or str(e)}
            task.status = TaskStatus.failed
            task.error_message = str(e)
            db.add(task)
            db.commit()
        return {"status": "error", "detail": str(e)}
    finally:
        db.close()


def _claim_outbox_worker_operation(
    event_id: str | None,
    *,
    worker_name: str,
    lease_seconds: int,
) -> tuple[str, str] | dict[str, Any] | None:
    if event_id is None:
        return None
    owner = f"{worker_name}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    db = _db()
    try:
        claim = claim_outbox_operation(db, event_id, owner, lease_seconds)
        if claim is None:
            return {"status": "error", "detail": "outbox event not found"}
        if not claim.acquired:
            if claim.result_json is not None:
                return claim.result_json
            return {"status": "in_progress", "operation_key": claim.operation.operation_key}
        db.commit()
        return claim.operation.operation_key, owner
    finally:
        db.close()


def _finish_outbox_worker_operation(operation_key: str, result: dict[str, Any]) -> None:
    db = _db()
    try:
        finish_operation(db, operation_key, result)
        db.commit()
    finally:
        db.close()


def _release_outbox_worker_operation(operation_key: str, owner: str, error: object) -> None:
    db = _db()
    try:
        release_operation(db, operation_key, owner, error)
        db.commit()
    finally:
        db.close()


@celery_app.task(
    name="subtitle_service.cancel_workflow_run",
    bind=True,
    max_retries=20,
    acks_late=True,
    reject_on_worker_lost=True,
)
def cancel_workflow_run(
    self: Any,
    pipeline_run_id: str,
    external_run_id: str,
    outbox_event_id: str | None = None,
) -> dict[str, Any]:
    """Durably cancel one Hatchet run after a VideoRoll task is stopped."""
    _ensure_db()
    operation = _claim_outbox_worker_operation(
        outbox_event_id,
        worker_name="subtitle_service.cancel_workflow_run",
        lease_seconds=120,
    )
    if isinstance(operation, dict):
        return operation
    if operation is None:
        raise ValueError("workflow cancellation must be delivered through the durable outbox")
    operation_key, owner = operation
    try:
        orchestrator_settings = get_orchestrator_settings()
        headers = {INTERNAL_TOKEN_HEADER: service_token(orchestrator_settings)}
        with httpx.Client(timeout=15.0, headers=headers) as client:
            response = client.post(
                f"{orchestrator_settings.workflow_service_url.rstrip('/')}/runs/{external_run_id}/cancel"
            )
            response.raise_for_status()
    except Exception as exc:
        _release_outbox_worker_operation(operation_key, owner, exc)
        raise self.retry(exc=exc, countdown=min(300, 2 ** min(int(self.request.retries or 0) + 1, 8)))

    db = _db()
    try:
        try:
            row = db.get(PipelineRun, uuid.UUID(str(pipeline_run_id)))
        except (TypeError, ValueError):
            row = None
        if row is not None:
            row.state = "canceled"
            row.error_message = None
            row.finished_at = _now()
            db.add(row)
            db.commit()
    finally:
        db.close()

    result = {
        "status": "ok",
        "pipeline_run_id": str(pipeline_run_id),
        "external_run_id": str(external_run_id),
    }
    _finish_outbox_worker_operation(operation_key, result)
    return result


@celery_app.task(
    name="subtitle_service.push_render_workflow_event",
    bind=True,
    max_retries=20,
    acks_late=True,
    reject_on_worker_lost=True,
)
def push_render_workflow_event(
    self: Any,
    render_job_id: str,
    status: str,
    execution_id: str,
    error: str | None = None,
    outbox_event_id: str | None = None,
) -> dict[str, Any]:
    """Durably bridge a committed render terminal state into Hatchet."""
    _ensure_db()
    operation = _claim_outbox_worker_operation(
        outbox_event_id,
        worker_name="subtitle_service.push_render_workflow_event",
        lease_seconds=120,
    )
    if isinstance(operation, dict):
        return operation
    if operation is None:
        raise ValueError("render workflow events must be delivered through the durable outbox")
    operation_key, owner = operation
    payload = {
        "render_job_id": str(render_job_id),
        "status": str(status),
        "execution_id": str(execution_id),
        "error": str(error) if error else None,
    }
    try:
        orchestrator_settings = get_orchestrator_settings()
        headers = {INTERNAL_TOKEN_HEADER: service_token(orchestrator_settings)}
        with httpx.Client(timeout=15.0, headers=headers) as client:
            response = client.post(
                f"{orchestrator_settings.workflow_service_url.rstrip('/')}/events/render-finished",
                json=payload,
            )
            response.raise_for_status()
    except Exception as exc:
        _release_outbox_worker_operation(operation_key, owner, exc)
        raise self.retry(exc=exc, countdown=min(300, 2 ** min(int(self.request.retries or 0) + 1, 8)))

    result = {
        "status": "ok",
        "render_status": str(status),
        "render_job_id": str(render_job_id),
        "execution_id": str(execution_id),
        "error": str(error) if error else None,
    }
    _finish_outbox_worker_operation(operation_key, result)
    return result


@celery_app.task(
    name="subtitle_service.push_publish_workflow_event",
    bind=True,
    max_retries=20,
    acks_late=True,
    reject_on_worker_lost=True,
)
def push_publish_workflow_event(
    self: Any,
    publish_batch_id: str,
    status: str,
    task_id: str,
    outbox_event_id: str | None = None,
) -> dict[str, Any]:
    """Durably bridge a committed publish-batch terminal state into Hatchet."""
    _ensure_db()
    operation = _claim_outbox_worker_operation(
        outbox_event_id,
        worker_name="subtitle_service.push_publish_workflow_event",
        lease_seconds=120,
    )
    if isinstance(operation, dict):
        return operation
    if operation is None:
        raise ValueError("publish workflow events must be delivered through the durable outbox")
    operation_key, owner = operation
    payload = {
        "publish_batch_id": str(publish_batch_id),
        "status": str(status),
        "task_id": str(task_id),
    }
    try:
        orchestrator_settings = get_orchestrator_settings()
        headers = {INTERNAL_TOKEN_HEADER: service_token(orchestrator_settings)}
        with httpx.Client(timeout=15.0, headers=headers) as client:
            response = client.post(
                f"{orchestrator_settings.workflow_service_url.rstrip('/')}/events/publish-finished",
                json=payload,
            )
            response.raise_for_status()
    except Exception as exc:
        _release_outbox_worker_operation(operation_key, owner, exc)
        raise self.retry(exc=exc, countdown=min(300, 2 ** min(int(self.request.retries or 0) + 1, 8)))

    result = {
        "status": "ok",
        "publish_status": str(status),
        "publish_batch_id": str(publish_batch_id),
        "task_id": str(task_id),
    }
    _finish_outbox_worker_operation(operation_key, result)
    return result


@celery_app.task(name="subtitle_service.after_render_publish")
def after_render_publish(render_job_id: str, outbox_event_id: str | None = None) -> dict[str, Any]:
    """Compatibility task name with optional durable-outbox consumption."""
    _ensure_db()
    operation = _claim_outbox_worker_operation(
        outbox_event_id,
        worker_name="subtitle_service.after_render_publish",
        lease_seconds=600,
    )
    if isinstance(operation, dict):
        return operation
    if operation is None:
        return _after_render_publish_impl(render_job_id)
    operation_key, owner = operation
    try:
        result = _after_render_publish_impl(render_job_id)
    except Retry as exc:
        _release_outbox_worker_operation(operation_key, owner, exc)
        raise
    _finish_outbox_worker_operation(operation_key, result)
    return result


@celery_app.task(name="subtitle_service.cleanup_task", bind=True, max_retries=20)
def cleanup_task(self: Any, task_id: str, batch_id: str | None = None, outbox_event_id: str | None = None) -> dict[str, Any]:
    """Compatibility task name with an inbox claim for outbox deliveries."""
    _ensure_db()
    operation = _claim_outbox_worker_operation(
        outbox_event_id,
        worker_name="subtitle_service.cleanup_task",
        lease_seconds=600,
    )
    if isinstance(operation, dict):
        return operation
    if operation is None:
        return _cleanup_task_impl(self, task_id, batch_id)
    operation_key, owner = operation
    try:
        result = _cleanup_task_impl(self, task_id, batch_id)
    except Retry as exc:
        _release_outbox_worker_operation(operation_key, owner, exc)
        raise
    if result.get("status") == "ok" and batch_id:
        db = _db()
        try:
            if mark_publish_batch_cleanup_enqueued(db, uuid.UUID(batch_id)):
                db.commit()
        finally:
            db.close()
    _finish_outbox_worker_operation(operation_key, result)
    return result


def _cleanup_task_impl(self: Any, task_id: str, batch_id: str | None = None) -> dict[str, Any]:
    """
    Best-effort cleanup after a task is published:
      - delete local WORK_DIR temp dirs for subtitle/render/youtube
      - delete non-final stored assets to prevent storage from growing forever
    Keeps:
      - AssetKind.video_final
      - AssetKind.publish_result
    """
    _ensure_db()
    store = FileStore(settings)
    store.ensure_ready()

    db = _db()
    tid: uuid.UUID | None = None
    try:
        tid = uuid.UUID(task_id)
        # Hold the same task-row lock used when a new publish batch/job is
        # created.  The asset decision and deletion must be one critical
        # section, otherwise a new job can lose its cover between the check.
        task = db.get(Task, tid, with_for_update=True)
        if not task:
            return {"status": "error", "detail": "task not found"}
        if batch_id:
            batch = db.get(PublishBatch, uuid.UUID(batch_id), with_for_update=True)
            if not batch or batch.task_id != tid:
                return {"status": "skipped", "detail": "publish batch not found for task"}
            if batch.state != "succeeded":
                return {"status": "skipped", "detail": f"publish batch state is {batch.state}"}
            if task.status != TaskStatus.published:
                return {"status": "skipped", "detail": f"task publish state is {task.status.value}"}
        elif task.status != TaskStatus.published:
            return {"status": "skipped", "detail": f"task status is {task.status.value}"}

        in_flight_publish = (
            db.query(PublishJob)
            .filter(PublishJob.task_id == tid, PublishJob.state == PublishState.submitting)
            .count()
        )
        if in_flight_publish:
            # Clear the outbox marker before Celery retry.  If retry delivery
            # itself fails, the periodic repair task can still enqueue cleanup.
            if batch_id:
                batch = db.get(PublishBatch, uuid.UUID(batch_id), with_for_update=True)
                if batch:
                    batch.cleanup_enqueued_at = None
                    db.add(batch)
                    db.commit()
            raise self.retry(countdown=60)

        in_flight_sub = (
            db.query(SubtitleJob)
            .filter(SubtitleJob.task_id == tid, SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]))
            .count()
        )
        in_flight_render = (
            db.query(RenderJob)
            .filter(RenderJob.task_id == tid, RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]))
            .count()
        )
        if in_flight_sub or in_flight_render:
            if batch_id:
                batch.cleanup_enqueued_at = None
                db.add(batch)
                db.commit()
            raise self.retry(countdown=60)

        subtitle_job_ids = [row[0] for row in db.query(SubtitleJob.id).filter(SubtitleJob.task_id == tid).all()]
        render_job_ids = [row[0] for row in db.query(RenderJob.id).filter(RenderJob.task_id == tid).all()]

        assets = db.query(Asset).filter(Asset.task_id == tid).all()
        keep_kinds = {AssetKind.video_final, AssetKind.publish_result}
        keep_keys = {a.storage_key for a in assets if a.kind in keep_kinds}

        if not any(a.kind == AssetKind.video_final for a in assets):
            # Safety: if there's no final video, keep the latest raw video asset (if any) to avoid deleting the only video.
            latest_raw = (
                db.query(Asset)
                .filter(Asset.task_id == tid, Asset.kind == AssetKind.video_raw)
                .order_by(Asset.created_at.desc(), Asset.id.desc())
                .first()
            )
            if latest_raw:
                keep_keys.add(latest_raw.storage_key)

        deleted_keys: list[str] = []
        for a in assets:
            if a.storage_key in keep_keys:
                continue
            try:
                store.delete_object(a.storage_key)
                deleted_keys.append(a.storage_key)
            except Exception:
                logger.exception("cleanup: failed to delete s3 object (task_id=%s key=%s)", task_id, a.storage_key)

        if deleted_keys:
            db.query(Subtitle).filter(Subtitle.task_id == tid, Subtitle.storage_key.in_(deleted_keys)).delete(synchronize_session=False)
            db.query(Asset).filter(Asset.task_id == tid, Asset.storage_key.in_(deleted_keys)).delete(synchronize_session=False)
            db.commit()

        for checkpoint_key in (
            f"sub/{tid}/translation_checkpoint.json",
            f"sub/{tid}/groq_asr_checkpoint.json",
        ):
            try:
                store.delete_object(checkpoint_key)
            except Exception:
                pass

        # Local temp dirs (WORK_DIR).
        work_dir = Path(settings.work_dir)
        removed_dirs = 0
        for sjid in subtitle_job_ids:
            p = work_dir / "subtitle" / str(sjid)
            try:
                if p.exists():
                    removed_dirs += 1
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
        for rjid in render_job_ids:
            p = work_dir / "render" / str(rjid)
            try:
                if p.exists():
                    removed_dirs += 1
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
        yt_dir = work_dir / "youtube" / str(tid)
        try:
            if yt_dir.exists():
                removed_dirs += 1
            shutil.rmtree(yt_dir, ignore_errors=True)
        except Exception:
            pass

        return {"status": "ok", "deleted_objects": str(len(deleted_keys)), "removed_dirs": str(removed_dirs)}
    except Retry:
        raise
    except Exception as e:
        logger.exception("cleanup task failed (task_id=%s)", task_id)
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


@celery_app.task(name="subtitle_service.enqueue_pending_publish_batch_cleanups")
def enqueue_pending_publish_batch_cleanups() -> dict[str, int]:
    """Repair legacy cleanup rows by inserting their idempotent outbox event."""
    _ensure_db()
    db = _db()
    enqueued = 0
    failed = 0
    try:
        batches = (
            db.query(PublishBatch)
            .filter(PublishBatch.state == "succeeded", PublishBatch.cleanup_enqueued_at.is_(None))
            .all()
        )
        for batch in batches:
            task = db.get(Task, batch.task_id)
            if not task or task.status != TaskStatus.published:
                continue
            if not any(current.id == batch.id for current in current_publish_batches_for_task(db, task.id)):
                continue
            try:
                if enqueue_publish_batch_cleanup(db, celery_app, task.id, batch.id, needed=True):
                    enqueued += 1
            except Exception:
                failed += 1
                logger.exception(
                    "failed to retry publish cleanup delivery (task_id=%s batch_id=%s)",
                    task.id,
                    batch.id,
                )
        return {"enqueued": enqueued, "failed": failed}
    finally:
        db.close()


@celery_app.task(name="subtitle_service.dispatch_outbox")
def dispatch_outbox() -> dict[str, int]:
    """Deliver every due durable event while retaining existing queue names."""
    _ensure_db()
    db = _db()
    try:
        result = dispatch_outbox_events(
            db,
            celery_app,
            owner=f"subtitle_service.dispatcher:{os.getpid()}:{uuid.uuid4().hex[:12]}",
            limit=50,
        )
        return {"claimed": result.claimed, "dispatched": result.dispatched, "failed": result.failed}
    finally:
        db.close()


@celery_app.task(name="subtitle_service.recover_publish_dispatches")
def recover_publish_dispatches() -> dict[str, int]:
    """Repair lost publisher delivery without guessing external outcomes."""
    _ensure_db()
    from videoroll.apps.publish_lifecycle import recover_stale_publish_dispatches

    db = _db()
    try:
        result = recover_stale_publish_dispatches(db, limit=100)
        db.commit()
        return {"requeued": result.requeued, "unknown": result.unknown}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
