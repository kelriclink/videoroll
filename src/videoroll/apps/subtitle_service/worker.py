from __future__ import annotations

import contextlib
import hashlib
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

import httpx
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
    AppSetting,
    Asset,
    AssetKind,
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
from videoroll.utils.task_queue import available_task_queue_capacity, task_queue_slot_reserved_for
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
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_summary
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
from videoroll.apps.publish_meta_draft import apply_publish_source_overrides, default_publish_meta
from videoroll.apps.outbox.dispatcher import dispatch_outbox_events
from videoroll.apps.outbox.service import create_outbox_event
from videoroll.apps.outbox.worker_inbox import (
    OperationHeartbeat,
    claim_operation,
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
from videoroll.apps.subtitle_service.render_queue_store import TASK_QUEUE_SETTINGS_KEY, get_task_queue_settings
from videoroll.apps.orchestrator_api.services import render_worker_service
from videoroll.apps.orchestrator_api.render_worker_schemas import ExecutionHeartbeatRequest
from videoroll.apps.subtitle_service.queues import SUBTITLE_CONTROL_QUEUE, SUBTITLE_WORK_QUEUE
from videoroll.apps.subtitle_service.worker_concurrency import (
    JobLeaseHeartbeat,
    acquire_job_lease,
    live_leased_task_ids,
    recover_expired_leases,
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
        "subtitle-service-task-queue-tick": {
            "task": "subtitle_service.task_queue_tick",
            "schedule": _TASK_QUEUE_TICK_INTERVAL_SECONDS,
            "args": (),
            "options": {"queue": SUBTITLE_CONTROL_QUEUE},
        },
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


# Task Queue (task-level concurrency)
TASK_QUEUE_LOCK_OWNER = "subtitle_service.task_queue"
_TASK_QUEUE_LOCK_TTL = timedelta(seconds=300)
_TASK_QUEUE_HEARTBEAT_INTERVAL_SECONDS = 30.0
_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS = 10
_JOB_LEASE_TTL_SECONDS = 900
_JOB_DISPATCH_PROGRESS = 1
_JOB_DISPATCH_RETRY_AFTER = timedelta(seconds=60)


def _task_queue_expires_at(now: datetime) -> datetime:
    return now + _TASK_QUEUE_LOCK_TTL


def _task_queue_is_task_locked(task: Task, now: datetime) -> bool:
    return bool(task.lock_owner == TASK_QUEUE_LOCK_OWNER and task.lock_until and task.lock_until > now)


def _task_queue_unlock(task: Task) -> None:
    task.lock_owner = None
    task.lock_until = None


def _kick_task_queue(*, countdown: int | None = None) -> None:
    options: dict[str, Any] = {"queue": SUBTITLE_CONTROL_QUEUE}
    if countdown is not None:
        options["countdown"] = countdown
    celery_app.send_task("subtitle_service.task_queue_tick", args=[], **options)


def _queued_job_dispatch_due(job: SubtitleJob | RenderJob, now: datetime) -> bool:
    if int(job.progress or 0) != _JOB_DISPATCH_PROGRESS:
        return True
    updated_at = job.updated_at
    if updated_at is None:
        return True
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at <= now - _JOB_DISPATCH_RETRY_AFTER


def _mark_queued_job_dispatched(job: SubtitleJob | RenderJob) -> None:
    job.progress = _JOB_DISPATCH_PROGRESS
    # A retry already has progress=1, so SQLAlchemy's onupdate would otherwise
    # see no change and leave the previous dispatch timestamp in place.
    job.updated_at = _now()


def _asr_cpu_threads(db: Session) -> int:
    configured = get_task_queue_settings(db)
    concurrency = max(1, min(32, int(configured.get("max_concurrency", 1))))
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
    if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
        _task_queue_unlock(task)
        db.add(task)
    db.commit()
    _kick_task_queue()
    return True


def _pause_render_job_if_task_stopped(db: Session, job_id: uuid.UUID) -> bool:
    job = db.get(RenderJob, job_id)
    if not job:
        return False
    if not _task_is_stopped(db, job.task_id):
        return False
    task = db.get(Task, job.task_id)
    if not task:
        return False
    db.refresh(task)
    if job.status == RenderJobStatus.running:
        job.status = RenderJobStatus.queued
        job.progress = 0
        job.started_at = None
        job.error_message = _task_queue_join_message(job.error_message, "Task stopped by user; waiting for resume.")
        db.add(job)
    if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
        _task_queue_unlock(task)
        db.add(task)
    db.commit()
    _kick_task_queue()
    return True


def _build_after_render_publish_action(
    *,
    task_id: uuid.UUID,
    cover_key: str | None,
    profile: dict[str, Any],
    yt_title: str,
    yt_desc: str,
    webpage_url: str,
    yt_uploader: str = "",
    db: Session,
    store: FileStore,
) -> dict[str, Any] | None:
    auto_publish_platforms = list(profile.get("auto_publish_platforms") or [])
    if not profile.get("auto_publish") or not auto_publish_platforms:
        return None

    meta = default_publish_meta(db)
    translate_settings = get_translate_settings(db, settings)
    draft_profile = dict(profile)
    if bool(profile.get("translate_enabled")) and bool(profile.get("publish_translate_title")):
        # The final title is generated after subtitle translation, when the
        # rolling summary is available. Keep this preliminary draft cheap and
        # overwrite it before the render-triggered publish action runs.
        draft_profile["publish_translate_title"] = False
    meta = apply_publish_source_overrides(
        meta,
        source_title=yt_title,
        source_description=yt_desc,
        source_url=webpage_url,
        source_uploader=yt_uploader,
        profile=draft_profile,
        translate_settings=translate_settings,
        summary=get_task_bilibili_summary(db, str(task_id)),
        ai_service=_ai_service(),
    )

    publish_meta_key = f"meta/{task_id}/publish_meta.json"
    store.put_bytes(
        json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
        publish_meta_key,
        content_type="application/json",
    )

    publish_payload = {
        "account_id": None,
        "platforms": auto_publish_platforms,
        "video_key": None,
        "cover_key": cover_key,
        "typeid_mode": profile.get("publish_typeid_mode") or "ai_summary",
        "meta": None,
    }
    return {"publish": True, "publish_payload": publish_payload}


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


def _task_queue_lock_settings_row(db: Session) -> None:
    """
    Serialize queue ticks by locking the settings row.
    This prevents overshooting max_concurrency when multiple ticks run concurrently.
    """
    row = db.get(AppSetting, TASK_QUEUE_SETTINGS_KEY)
    if not row:
        row = AppSetting(key=TASK_QUEUE_SETTINGS_KEY, value_json={})
        db.add(row)
        db.commit()
    # Best-effort lock (ignored on dialects that don't support it).
    db.query(AppSetting).filter(AppSetting.key == TASK_QUEUE_SETTINGS_KEY).with_for_update().first()


def _task_has_queued_or_running_jobs(db: Session, task_id: uuid.UUID) -> bool:
    subtitle_jobs = (
        db.query(SubtitleJob)
        .filter(SubtitleJob.task_id == task_id, SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]))
        .count()
    )
    if int(subtitle_jobs or 0) > 0:
        return True
    render_jobs = (
        db.query(RenderJob)
        .filter(RenderJob.task_id == task_id, RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]))
        .count()
    )
    return bool(int(render_jobs or 0) > 0)


def _cancel_unclaimable_render_job(db: Session, job: RenderJob, task: Task, now: datetime) -> str | None:
    """Cancel terminal-task renders and duplicate active renders before FFmpeg starts."""
    if task.status == TaskStatus.published:
        job.status = RenderJobStatus.canceled
        job.progress = 0
        job.finished_at = now
        job.lease_owner = None
        job.lease_until = None
        job.heartbeat_at = now
        job.error_message = _task_queue_join_message(
            job.error_message,
            "Task is already published; stale render job canceled.",
        )
        if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
            _task_queue_unlock(task)
        db.add(job)
        db.add(task)
        return "task already published"

    active_jobs = (
        db.query(RenderJob)
        .filter(
            RenderJob.task_id == task.id,
            RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
        )
        .order_by(RenderJob.created_at.asc(), RenderJob.id.asc())
        .all()
    )
    live_jobs = [
        active
        for active in active_jobs
        if active.status == RenderJobStatus.running and active.lease_until is not None and active.lease_until > now
    ]
    canonical = live_jobs[0] if live_jobs else (active_jobs[0] if active_jobs else None)
    if canonical is None or canonical.id == job.id:
        return None

    job.status = RenderJobStatus.canceled
    job.progress = 0
    job.finished_at = now
    job.lease_owner = None
    job.lease_until = None
    job.heartbeat_at = now
    job.error_message = _task_queue_join_message(
        job.error_message,
        f"Duplicate render job canceled; active render job is {canonical.id}.",
    )
    db.add(job)
    return f"superseded by render job {canonical.id}"


class _TaskQueueHeartbeat:
    def __init__(self, task_id: uuid.UUID):
        self._task_id = task_id
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, name=f"task-queue-hb-{task_id}", daemon=True)

    def start(self) -> None:
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._thr.join(timeout=5.0)
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.wait(_TASK_QUEUE_HEARTBEAT_INTERVAL_SECONDS):
            db = _db()
            try:
                now = _now()
                db.query(Task).filter(Task.id == self._task_id, Task.lock_owner == TASK_QUEUE_LOCK_OWNER).update(
                    {"lock_until": _task_queue_expires_at(now)},
                    synchronize_session=False,
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                db.close()


class _RenderExecutionHeartbeat:
    def __init__(self, execution_id: uuid.UUID, fence_token: str):
        self._execution_id = execution_id
        self._fence_token = fence_token
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, name=f"render-exec-hb-{execution_id}", daemon=True)

    def start(self) -> None:
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._thr.join(timeout=5.0)
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.wait(30.0):
            db = _db()
            try:
                execution = render_worker_service.get_execution(db, self._execution_id)
                job = db.get(RenderJob, execution.render_job_id)
                render_worker_service.heartbeat_execution(
                    db, self._execution_id,
                    ExecutionHeartbeatRequest(
                        fence_token=self._fence_token,
                        progress=int(job.progress or 0) if job is not None else None,
                        metrics={},
                    ),
                )
            except Exception:
                try: db.rollback()
                except Exception: pass
                logger.exception("failed to heartbeat local render execution %s", self._execution_id)
            finally:
                db.close()


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
    """Initialize runtime state and let the scheduler recover expired leases."""
    # Validate production service identity during an actual worker start, not
    # while this module is imported by offline tooling or tests.
    _orchestrator_internal_headers()
    try:
        _ensure_db()
        _kick_task_queue()
    except Exception:
        logger.exception("subtitle worker initialization failed")


@celery_app.task(name="subtitle_service.process_job", bind=True, acks_late=True, reject_on_worker_lost=True)
def process_job(self: Any, job_id: str) -> dict[str, str]:
    _ensure_db()
    store = FileStore(settings)
    store.ensure_ready()

    jid = uuid.UUID(job_id)
    db = _db()
    log_path: Path | None = None
    log_key: str | None = None
    hb: _TaskQueueHeartbeat | None = None
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
            if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
                _task_queue_unlock(task)
            db.add(job)
            db.add(task)
            db.commit()
            return {"status": "skipped", "detail": "task already published"}
        if _pause_subtitle_job_if_task_stopped(db, jid):
            return {"status": "stopped", "detail": "task stopped by user"}

        now = _now()
        if job.status == SubtitleJobStatus.running:
            return {"status": "in_progress", "detail": "running job awaits completion or lease recovery"}
        # Serialize worker claiming with dispatch so duplicate broker deliveries
        # cannot start the same queued job twice.
        _task_queue_lock_settings_row(db)
        db.refresh(job)
        db.refresh(task)
        if job.status != SubtitleJobStatus.queued:
            return {"status": "in_progress", "detail": "job was already claimed or completed"}
        if task.lock_owner != TASK_QUEUE_LOCK_OWNER:
            # Do not rewrite an existing running row here.  Only the lease
            # recovery scheduler may decide that a worker is dead.
            if job.status == SubtitleJobStatus.running:
                return {"status": "in_progress", "detail": "running job awaits lease recovery"}
            _kick_task_queue(countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS)
            return {"status": "queued", "detail": "waiting for task queue"}
        if task.lock_until is None or task.lock_until <= now:
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)

        # progress=1 is set by the scheduler; bump to >=2 ASAP to mark as claimed by a worker.
        job.status = SubtitleJobStatus.running
        job.progress = max(int(job.progress or 0), 2)
        db.add(job)
        db.flush()
        candidate_owner = f"subtitle_service.process_job:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        if not acquire_job_lease(db, job, candidate_owner, _JOB_LEASE_TTL_SECONDS):
            db.rollback()
            return {"status": "in_progress", "detail": "job lease is held by another worker"}
        db.commit()
        lease_owner = candidate_owner

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()
        job_hb = JobLeaseHeartbeat(lambda: _db(), job.id, lease_owner, _JOB_LEASE_TTL_SECONDS)
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
                unlock_task=_task_queue_unlock,
                kick_task_queue=_kick_task_queue,
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
                retry_attempt=int(getattr(self.request, "retries", 0) or 0),
                database_url=settings.database_url,
                fresh_translate_settings=_fresh_translate_settings,
                ai_service_factory=_ai_service,
                log=lambda message: _safe_append_log_line(log_path, message),
            )
        except TranslationRetryRequired as retry:
            req["resume"] = True
            _save_job_request()
            job.error_message = (
                f"translate failed; celery retrying "
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
            raise self.retry(
                exc=retry.cause,
                countdown=retry.countdown,
                max_retries=retry.max_retries,
            )

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
            unlock_task=_task_queue_unlock,
            kick_task_queue=_kick_task_queue,
            log=lambda message: _safe_append_log_line(log_path, message),
            upload_log=lambda: _safe_upload_log(store, log_path, log_key),
        )
    except _TaskStopped:
        _pause_subtitle_job_if_task_stopped(db, jid)
        return {"status": "stopped", "detail": "task stopped by user"}
    except Retry:
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
            if task and task.lock_owner == TASK_QUEUE_LOCK_OWNER:
                _task_queue_unlock(task)
                db.add(task)
            if task and not _task_is_stopped(db, task.id) and task.status != TaskStatus.published:
                task.status = TaskStatus.failed
                task.error_code = task.error_code or "SUBTITLE_FAILED"
                task.error_message = str(e)
                db.add(task)
        db.commit()
        _safe_append_log_line(log_path, f"ERROR: {type(e).__name__}: {e}")
        _safe_append_log_block(log_path, traceback.format_exc())
        _safe_upload_log(store, log_path, log_key)
        _kick_task_queue()
        return {"status": "error", "detail": str(e)}
    finally:
        if job_hb is not None:
            job_hb.stop()
        if hb is not None:
            hb.stop()
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


@celery_app.task(name="subtitle_service.task_queue_tick")
def task_queue_tick() -> dict[str, Any]:
    """
    Task-level scheduler.

    max_concurrency now limits the number of *tasks* (pipelines) that can be in-flight.
    A task occupies a slot from subtitle-job start until render finishes (or subtitle finishes
    when no render is configured).
    """
    _ensure_db()
    db = _db()
    now = _now()

    started_subtitle = 0
    started_render = 0
    recovered_subtitle = 0
    recovered_render = 0
    recovered_pipeline = 0
    unlocked_expired = 0

    to_start: list[tuple[str, str]] = []  # ("subtitle"|"render", job_id)
    to_bootstrap: list[tuple[str, dict[str, Any] | None]] = []
    try:
        _task_queue_lock_settings_row(db)
        cfg = get_task_queue_settings(db)
        try:
            max_conc = int(cfg.get("max_concurrency", 1))
        except Exception:
            max_conc = 1
        if max_conc < 0:
            max_conc = 0
        recovery = recover_expired_leases(db, now=now, limit=100)
        recovered_subtitle = recovery.subtitle_requeued
        recovered_render = recovery.render_requeued
        live_job_task_ids = live_leased_task_ids(db, now)
        if max_conc == 0:
            db.commit()
            return {
                "status": "paused",
                "max_concurrency": str(max_conc),
                "recovered_subtitle": str(recovered_subtitle),
                "recovered_render": str(recovered_render),
            }

        # Clear expired locks to avoid permanent stalls after crashes.
        try:
            unlocked_expired = int(
                db.query(Task)
                .filter(Task.lock_owner == TASK_QUEUE_LOCK_OWNER, Task.lock_until.is_not(None), Task.lock_until <= now)
                .update({"lock_owner": None, "lock_until": None}, synchronize_session=False)
                or 0
            )
        except Exception:
            unlocked_expired = 0

        # A stop request clears the lock in the orchestrator, but this also
        # repairs terminal-task locks left by an older deployment or tick.
        try:
            unlocked_expired += int(
                db.query(Task)
                .filter(
                    Task.status.in_([TaskStatus.canceled, TaskStatus.published]),
                    Task.lock_owner == TASK_QUEUE_LOCK_OWNER,
                )
                .update({"lock_owner": None, "lock_until": None}, synchronize_session=False)
                or 0
            )
        except Exception:
            pass

        unlocked = or_(
            Task.lock_owner != TASK_QUEUE_LOCK_OWNER,
            Task.lock_until.is_(None),
            Task.lock_until <= now,
        )
        schedulable_unlocked = unlocked
        if live_job_task_ids:
            schedulable_unlocked = unlocked & Task.id.notin_(live_job_task_ids)

        locked_tasks = (
            db.query(Task)
            .filter(
                Task.status.notin_([TaskStatus.canceled, TaskStatus.published]),
                Task.lock_owner == TASK_QUEUE_LOCK_OWNER,
                Task.lock_until.is_not(None),
                Task.lock_until > now,
            )
            .order_by(Task.lock_until.asc())
            .all()
        )
        dispatch_retry_cutoff = now - _JOB_DISPATCH_RETRY_AFTER

        # Phase 1: advance locked tasks (start their next queued job if nothing is running).
        for t in locked_tasks:
            tid = t.id
            has_running = (
                db.query(SubtitleJob).filter(SubtitleJob.task_id == tid, SubtitleJob.status == SubtitleJobStatus.running).count()
                + db.query(RenderJob).filter(RenderJob.task_id == tid, RenderJob.status == RenderJobStatus.running).count()
            )
            if has_running:
                continue

            sj = (
                db.query(SubtitleJob)
                .filter(SubtitleJob.task_id == tid, SubtitleJob.status == SubtitleJobStatus.queued)
                .order_by(SubtitleJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if sj and _queued_job_dispatch_due(sj, now):
                _mark_queued_job_dispatched(sj)
                db.add(sj)
                to_start.append(("subtitle", str(sj.id)))
                started_subtitle += 1

        # Phase 2: choose the next *task* globally, then advance that task's
        # next stage.  Priority therefore works across subtitle, render, and
        # recoverable auto-YouTube pipelines instead of only within one job
        # type.
        running_tasks = len({task.id for task in locked_tasks} | live_job_task_ids)
        capacity = available_task_queue_capacity(max_conc, running_tasks)
        bootstrap_cutoff = now - timedelta(seconds=60)

        render_due_exists = (
            db.query(RenderJob.id)
            .filter(
                RenderJob.task_id == Task.id,
                RenderJob.status == RenderJobStatus.queued,
                or_(RenderJob.progress != _JOB_DISPATCH_PROGRESS, RenderJob.updated_at <= dispatch_retry_cutoff),
            )
            .exists()
        )
        subtitle_due_exists = (
            db.query(SubtitleJob.id)
            .filter(
                SubtitleJob.task_id == Task.id,
                SubtitleJob.status == SubtitleJobStatus.queued,
                or_(SubtitleJob.progress != _JOB_DISPATCH_PROGRESS, SubtitleJob.updated_at <= dispatch_retry_cutoff),
            )
            .exists()
        )
        any_subtitle_job_exists = db.query(SubtitleJob.id).filter(SubtitleJob.task_id == Task.id).exists()
        any_render_job_exists = db.query(RenderJob.id).filter(RenderJob.task_id == Task.id).exists()
        auto_youtube_origin = or_(
            Task.created_by == "auto_youtube",
            Task.created_by.like("auto_youtube;%"),
            Task.created_by == "youtube_home_scan",
            Task.created_by.like("youtube_home_scan;%"),
            Task.created_by == "youtube_task_restart",
            Task.created_by.like("youtube_task_restart;%"),
        )
        recoverable_pipeline = (
            (Task.source_type == SourceType.youtube)
            & Task.status.in_([TaskStatus.ingested, TaskStatus.downloaded])
            & Task.updated_at.is_not(None)
            & (Task.updated_at < bootstrap_cutoff)
            & auto_youtube_origin
            & ~any_subtitle_job_exists
            & ~any_render_job_exists
        )

        for _ in range(capacity):
            if available_task_queue_capacity(max_conc, running_tasks) <= 0:
                break
            # Production sessions disable autoflush. Persist this tick's
            # previous claims before selecting another unlocked candidate.
            db.flush()
            task = (
                db.query(Task)
                .filter(
                    Task.status.notin_([TaskStatus.canceled, TaskStatus.published]),
                    schedulable_unlocked,
                    or_(subtitle_due_exists, recoverable_pipeline),
                )
                .order_by(Task.priority.desc(), Task.queue_position.asc().nullslast(), Task.created_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if task is None:
                break
            if _task_queue_is_task_locked(task, now):
                continue
            if task.lock_until and task.lock_until > now and task.lock_owner and task.lock_owner != TASK_QUEUE_LOCK_OWNER:
                continue

            sj = (
                db.query(SubtitleJob)
                .filter(
                    SubtitleJob.task_id == task.id,
                    SubtitleJob.status == SubtitleJobStatus.queued,
                    or_(SubtitleJob.progress != _JOB_DISPATCH_PROGRESS, SubtitleJob.updated_at <= dispatch_retry_cutoff),
                )
                .order_by(SubtitleJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )

            task.lock_owner = TASK_QUEUE_LOCK_OWNER
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)

            if sj is not None:
                _mark_queued_job_dispatched(sj)
                db.add(sj)
                to_start.append(("subtitle", str(sj.id)))
                started_subtitle += 1
                running_tasks += 1
                continue

            meta = parse_auto_youtube_created_by(task.created_by)
            if meta is None:
                # Defensive: the SQL candidate predicate should make this
                # impossible, but never reserve a slot for an invalid source.
                _task_queue_unlock(task)
                db.add(task)
                continue
            overrides: dict[str, Any] | None = None
            if meta.get("auto_publish") is not None:
                overrides = {"auto_publish": bool(meta["auto_publish"])}
            to_bootstrap.append((str(task.id), overrides))
            recovered_pipeline += 1
            running_tasks += 1

        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()

    if unlocked_expired:
        publish_queue_changed(settings.redis_url)

    for kind, jid in to_start:
        if kind == "subtitle":
            celery_app.send_task("subtitle_service.process_job", args=[jid], queue="subtitle")
    for task_id, overrides in to_bootstrap:
        task_args: list[Any] = [task_id]
        if isinstance(overrides, dict) and overrides:
            task_args.append(dict(overrides))
        celery_app.send_task("subtitle_service.auto_youtube_pipeline", args=task_args, queue="subtitle")

    return {
        "status": "ok",
        "max_concurrency": str(max_conc),
        "started_subtitle": str(started_subtitle),
        "started_render": str(started_render),
        "recovered_subtitle": str(recovered_subtitle),
        "recovered_render": str(recovered_render),
        "recovered_pipeline": str(recovered_pipeline),
        "unlocked_expired": str(unlocked_expired),
    }


@celery_app.task(name="subtitle_service.render_coordinator_tick")
def render_coordinator_tick() -> dict[str, Any]:
    """Compatibility no-op after local rendering moved to standalone workers."""
    return {"status": "standalone-workers", "claimed": "0"}


@celery_app.task(name="subtitle_service.render_queue_tick")
def render_queue_tick() -> dict[str, Any]:
    return render_coordinator_tick()


@celery_app.task(name="subtitle_service.process_render_job", bind=True, acks_late=True, reject_on_worker_lost=True)
def process_render_job(self: Any, render_job_id: str, execution_id: str | None = None, fence_token: str | None = None) -> dict[str, Any]:
    _ensure_db()
    store = FileStore(settings)
    store.ensure_ready()

    rid = uuid.UUID(render_job_id)
    db = _db()
    log_path: Path | None = None
    log_key: str | None = None
    hb: _TaskQueueHeartbeat | None = None
    job_hb: JobLeaseHeartbeat | None = None
    execution_hb: _RenderExecutionHeartbeat | None = None
    lease_owner: str | None = None
    execution_uuid = uuid.UUID(execution_id) if execution_id else None
    coordinator_owned = execution_uuid is not None and bool(fence_token)
    work_root: Path | None = None
    try:
        rj = db.get(RenderJob, rid)
        if not rj:
            return {"status": "error", "detail": "render job not found"}
        if not coordinator_owned:
            # Old deployments may still have a queued Celery render message.
            # Never let it bypass the Render Coordinator after the cutover.
            return {"status": "skipped", "detail": "render job must be assigned by render coordinator"}

        task = db.query(Task).filter(Task.id == rj.task_id).with_for_update().first()
        if not task:
            return {"status": "error", "detail": "task not found"}
        db.refresh(rj)
        if rj.status == RenderJobStatus.succeeded:
            return {"status": "ok", "detail": "already succeeded"}
        if rj.status == RenderJobStatus.canceled:
            return {"status": "skipped", "detail": "canceled"}
        if _pause_render_job_if_task_stopped(db, rid):
            return {"status": "stopped", "detail": "task stopped by user"}

        now = _now()
        skip_detail = _cancel_unclaimable_render_job(db, rj, task, now)
        if skip_detail:
            db.commit()
            return {"status": "skipped", "detail": skip_detail}
        expected_owner = f"{render_worker_service.REMOTE_LOCK_PREFIX}{execution_uuid}" if coordinator_owned else None
        if coordinator_owned and rj.lease_owner != expected_owner:
            return {"status": "skipped", "detail": "render coordinator ownership lost"}
        if not coordinator_owned and rj.status == RenderJobStatus.running and rj.lease_until is not None and rj.lease_until > now:
            return {"status": "in_progress", "detail": "render job has a live worker lease"}
        if not coordinator_owned and task.lock_owner != TASK_QUEUE_LOCK_OWNER:
            # As with subtitle work, only expired leases may move a running
            # render back to queued.
            if rj.status == RenderJobStatus.running:
                return {"status": "in_progress", "detail": "running render awaits lease recovery"}
            _kick_task_queue(countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS)
            return {"status": "queued", "detail": "waiting for task queue"}
        if not coordinator_owned and (task.lock_until is None or task.lock_until <= now):
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)
            db.commit()

        # Best-effort: if called directly, atomically claim it before render work.
        if not coordinator_owned and rj.status == RenderJobStatus.queued:
            rj.status = RenderJobStatus.running
            rj.started_at = _now()
        if rj.status == RenderJobStatus.running and rj.started_at is None:
            rj.started_at = _now()

        if rj.status != RenderJobStatus.running:
            return {"status": "skipped", "detail": f"unexpected status={rj.status.value}"}

        # Mark as claimed by a worker ASAP so the scheduler can detect orphaned jobs.
        rj.progress = max(int(rj.progress or 0), 2)
        db.add(rj)
        db.flush()
        if coordinator_owned:
            lease_owner = expected_owner
            render_worker_service.heartbeat_execution(
                db, execution_uuid,
                ExecutionHeartbeatRequest(fence_token=str(fence_token), progress=max(int(rj.progress or 0), 2), metrics={}),
            )
        else:
            candidate_owner = f"subtitle_service.process_render_job:{os.getpid()}:{uuid.uuid4().hex[:12]}"
            if not acquire_job_lease(db, rj, candidate_owner, _JOB_LEASE_TTL_SECONDS):
                db.rollback()
                return {"status": "in_progress", "detail": "render job lease is held by another worker"}
            db.commit()
            lease_owner = candidate_owner

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()
        if not coordinator_owned:
            job_hb = JobLeaseHeartbeat(lambda: _db(), rj.id, lease_owner, _JOB_LEASE_TTL_SECONDS)
            job_hb.start()
        else:
            execution_hb = _RenderExecutionHeartbeat(execution_uuid, str(fence_token))
            execution_hb.start()

        _raise_if_task_stopped(db, task.id)

        req = rj.request_json if isinstance(rj.request_json, dict) else {}
        input_key = str(req.get("input_key") or "").strip()
        srt_key = str(req.get("srt_key") or "").strip()
        ass_key = str(req.get("ass_key") or "").strip() or None
        automatic_render = _uses_runtime_auto_profile(task, req)
        if automatic_render:
            # Render is its own configurable box.  Old queued render rows may
            # contain stale snapshots; ignore them for automatic tasks and read
            # the current profile exactly when this worker claims the stage.
            render_profile = dict(get_auto_profile(db))
            burn_in = bool(render_profile.get("burn_in"))
            soft_sub = bool(render_profile.get("soft_sub"))
            render_cfg = {
                "video_codec": render_profile.get("video_codec") or "av1",
                "use_intel_gpu": bool(render_profile.get("use_intel_gpu")),
                "video_preset": render_profile.get("video_preset"),
                "video_crf": render_profile.get("video_crf"),
                "ass_style": render_profile.get("ass_style") or "clean_white",
                "primary_font_scale_percent": render_profile.get("primary_font_scale_percent") or 100,
                "secondary_font_scale_percent": render_profile.get("secondary_font_scale_percent") or 100,
            }
        else:
            burn_in = bool(req.get("burn_in"))
            soft_sub = bool(req.get("soft_sub"))
            render_cfg = req.get("render") if isinstance(req.get("render"), dict) else {}

        video_codec = str(render_cfg.get("video_codec") or "av1").strip().lower() or "av1"
        use_intel_gpu = bool(render_cfg.get("use_intel_gpu"))
        video_preset = render_cfg.get("video_preset")
        # Wire compatibility keeps the historical video_crf name; internally it
        # is encoder quality (CRF for software encoders, QP/global_quality for QSV).
        video_quality = render_cfg.get("video_crf")

        if not input_key:
            raise ValueError("render job missing input_key")
        if not srt_key:
            raise ValueError("render job missing srt_key")
        if burn_in and not automatic_render and not ass_key:
            raise ValueError("render job missing ass_key for burn_in")

        work_root = Path(settings.work_dir) / "render" / str(rj.id)
        work_root.mkdir(parents=True, exist_ok=True)
        log_path = work_root / "job.log"
        log_key = f"log/{task.id}/render_{rj.id}.log"
        try:
            _ensure_log_asset(db, task.id, log_key)
            db.commit()
        except Exception:
            db.rollback()

        _seed_log_from_store(store, log_key, log_path)
        _safe_append_log_line(
            log_path,
            f"render job start: render_job_id={rj.id} task_id={task.id} burn_in={burn_in} soft_sub={soft_sub} codec={video_codec} intel_gpu={use_intel_gpu} preset={video_preset} quality={video_quality}",
        )
        _safe_upload_log(store, log_path, log_key)

        last_live_upload_at = 0.0
        last_live_upload_size = -1
        last_db_heartbeat_at = 0.0

        def _heartbeat_db(now: float) -> None:
            nonlocal last_db_heartbeat_at
            if now - last_db_heartbeat_at < 10.0:
                return
            try:
                rj.updated_at = _now()
                db.add(rj)
                db.commit()
                last_db_heartbeat_at = now
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        def _live_upload_log() -> None:
            nonlocal last_live_upload_at, last_live_upload_size
            if log_path is None or not log_key:
                return
            try:
                now = time.monotonic()
                _heartbeat_db(now)
                if now - last_live_upload_at < 2.0:
                    return
                size = log_path.stat().st_size if log_path.exists() else 0
                if last_live_upload_size >= 0 and size - last_live_upload_size < 4096 and now - last_live_upload_at < 10.0:
                    return
                _safe_upload_log(store, log_path, log_key)
                last_live_upload_at = now
                last_live_upload_size = size
            except Exception:
                pass

        video_path = store.path_for(input_key)
        srt_path = store.path_for(srt_key)
        ass_path = (
            work_root / "subtitle_runtime.ass"
            if automatic_render and burn_in
            else (store.path_for(ass_key) if burn_in and ass_key else work_root / "subtitle_zh.ass")
        )

        _safe_append_log_line(log_path, f"storage input ready: input_key={input_key}")
        _safe_upload_log(store, log_path, log_key)
        rj.progress = max(int(rj.progress or 0), 5)
        db.add(rj)
        db.commit()
        _safe_append_log_line(log_path, f"storage subtitle ready: srt_key={srt_key}")
        _safe_upload_log(store, log_path, log_key)
        if burn_in and ass_key:
            _safe_append_log_line(log_path, f"storage ASS ready: ass_key={ass_key}")
            _safe_upload_log(store, log_path, log_key)
        _safe_append_log_line(log_path, "storage inputs ready")
        _safe_upload_log(store, log_path, log_key)

        subtitle_job: SubtitleJob | None = None
        if rj.subtitle_job_id:
            subtitle_job = db.get(SubtitleJob, rj.subtitle_job_id)
            if subtitle_job:
                subtitle_job.progress = max(int(subtitle_job.progress or 0), 81)
                db.add(subtitle_job)

        if automatic_render and burn_in:
            runtime_segments: list[Segment] = []
            if subtitle_job and isinstance(subtitle_job.request_json, dict):
                artifacts = subtitle_job.request_json.get("artifacts")
                if isinstance(artifacts, dict):
                    segments_key = str(artifacts.get("final_subtitle_segments_key") or "").strip()
                    if segments_key:
                        try:
                            segments_payload = json.loads(store.path_for(segments_key).read_text(encoding="utf-8"))
                            runtime_segments = segments_from_json_data(segments_payload)
                        except Exception:
                            runtime_segments = []
            if not runtime_segments:
                runtime_segments = srt_to_segments(srt_path.read_text(encoding="utf-8"))
            play_res_x, play_res_y = probe_video_resolution(settings.ffmpeg_path, video_path)
            secondary_line_scale = 0.68 if any(seg.secondary_text for seg in runtime_segments) else None
            ass_path.write_text(
                segments_to_ass(
                    runtime_segments,
                    style_name=str(render_cfg.get("ass_style") or "clean_white"),
                    play_res_x=play_res_x,
                    play_res_y=play_res_y,
                    secondary_line_scale=secondary_line_scale,
                    primary_font_scale_percent=int(render_cfg.get("primary_font_scale_percent") or 100),
                    secondary_font_scale_percent=int(render_cfg.get("secondary_font_scale_percent") or 100),
                ),
                encoding="utf-8",
            )
            _safe_append_log_line(log_path, "runtime ASS generated from current render box settings")

        rj.progress = max(int(rj.progress or 0), 10)
        db.add(rj)
        db.commit()

        if burn_in:
            rj.progress = max(int(rj.progress or 0), 20)
            if subtitle_job:
                subtitle_job.progress = max(int(subtitle_job.progress or 0), 85)
                db.add(subtitle_job)
            db.add(rj)
            db.commit()

            out_video = work_root / "video_burnin.mp4"
            _safe_append_log_line(log_path, "ffmpeg: burn-in subtitles")
            render_burn_in(
                settings.ffmpeg_path,
                video_path,
                ass_path,
                out_video,
                video_codec=video_codec,
                use_intel_gpu=use_intel_gpu,
                intel_gpu_render_device=settings.intel_gpu_render_device,
                preset=video_preset,
                crf=video_quality,
                log_path=log_path,
                live_upload_cb=_live_upload_log,
            )
            _safe_upload_log(store, log_path, log_key)
            final_sha = sha256_file(out_video)
            final_key = _unique_storage_key(
                f"final/{task.id}/video_burnin",
                final_sha,
                ".mp4",
            )
            final_size = out_video.stat().st_size
            store.promote_file(out_video, final_key)
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.video_final,
                    storage_key=final_key,
                    sha256=final_sha,
                    size_bytes=final_size,
                )
            )

        if soft_sub:
            rj.progress = max(int(rj.progress or 0), 60)
            if subtitle_job:
                subtitle_job.progress = max(int(subtitle_job.progress or 0), 90)
                db.add(subtitle_job)
            db.add(rj)
            db.commit()

            out_video = work_root / "video_softsub.mkv"
            _safe_append_log_line(log_path, "ffmpeg: mux soft subtitles")
            mux_soft_sub(settings.ffmpeg_path, video_path, srt_path, out_video, log_path=log_path, live_upload_cb=_live_upload_log)
            _safe_upload_log(store, log_path, log_key)
            final_sha = sha256_file(out_video)
            final_key = _unique_storage_key(
                f"final/{task.id}/video_softsub",
                final_sha,
                ".mkv",
            )
            final_size = out_video.stat().st_size
            store.promote_file(out_video, final_key)
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.video_final,
                    storage_key=final_key,
                    sha256=final_sha,
                    size_bytes=final_size,
                )
            )

        _raise_if_task_stopped(db, task.id)
        if burn_in or soft_sub:
            task.status = TaskStatus.rendered
            db.add(task)

        if subtitle_job:
            subtitle_job.status = SubtitleJobStatus.succeeded
            subtitle_job.progress = 100
            subtitle_job.error_message = None
            db.add(subtitle_job)

        rj.status = RenderJobStatus.succeeded
        rj.progress = 100
        rj.finished_at = _now()
        db.add(rj)
        # The completed render and its auto-publish instruction are one
        # transaction.  The dispatcher, rather than this worker, performs the
        # broker delivery so a broker outage cannot lose auto publishing.
        after_render = req.get("after_render") if isinstance(req, dict) else None
        if automatic_render or (isinstance(after_render, dict) and after_render.get("publish")):
            create_outbox_event(
                db,
                event_type="render.after_publish",
                aggregate_type="render_job",
                aggregate_id=rj.id,
                task_name="subtitle_service.after_render_publish",
                args={"args": [str(rj.id)], "queue": "subtitle"},
                operation_key=f"after-render-publish:{rj.id}",
            )
        db.commit()
        if coordinator_owned:
            if execution_hb is not None:
                execution_hb.stop()
                execution_hb = None
            render_worker_service.settle_local_execution(db, execution_uuid, str(fence_token), succeeded=True)
        _safe_append_log_line(log_path, "render job done")
        _safe_upload_log(store, log_path, log_key)

        if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
            _task_queue_unlock(task)
            db.add(task)
            db.commit()

        _kick_task_queue()
        return {"status": "ok"}
    except _TaskStopped:
        _pause_render_job_if_task_stopped(db, rid)
        return {"status": "stopped", "detail": "task stopped by user"}
    except Retry:
        retry_job = db.get(RenderJob, rid)
        if retry_job and retry_job.status == RenderJobStatus.running:
            retry_job.status = RenderJobStatus.queued
            db.add(retry_job)
            db.commit()
        raise
    except Exception as e:
        rj = db.get(RenderJob, rid)
        if rj:
            rj.status = RenderJobStatus.failed
            rj.error_message = str(e)
            rj.finished_at = _now()
            db.add(rj)
            task = db.get(Task, rj.task_id)
            if task:
                if not _task_is_stopped(db, task.id) and task.status != TaskStatus.published:
                    task.status = TaskStatus.failed
                    task.error_code = task.error_code or "RENDER_FAILED"
                    task.error_message = str(e)
                if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
                    _task_queue_unlock(task)
                db.add(task)
        if rj and rj.subtitle_job_id:
            sj = db.get(SubtitleJob, rj.subtitle_job_id)
            if sj:
                sj.status = SubtitleJobStatus.failed
                sj.error_message = f"render failed: {e}"
                db.add(sj)
        db.commit()
        if coordinator_owned:
            if execution_hb is not None:
                execution_hb.stop()
                execution_hb = None
            try:
                render_worker_service.settle_local_execution(
                    db, execution_uuid, str(fence_token), succeeded=False, error=str(e)
                )
            except Exception:
                logger.exception("failed to settle local render execution %s", execution_uuid)
        _safe_append_log_line(log_path, f"ERROR: {type(e).__name__}: {e}")
        _safe_append_log_block(log_path, traceback.format_exc())
        _safe_upload_log(store, log_path, log_key)
        _kick_task_queue()
        return {"status": "error", "detail": str(e)}
    finally:
        if execution_hb is not None:
            execution_hb.stop()
        if job_hb is not None:
            job_hb.stop()
        if hb is not None:
            hb.stop()
        if lease_owner is not None and not coordinator_owned:
            lease_db = _db()
            try:
                release_job_lease(lease_db, rid, lease_owner)
                lease_db.commit()
            except Exception:
                lease_db.rollback()
                logger.exception("failed to release render job lease (job_id=%s)", rid)
            finally:
                lease_db.close()
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
        automatic_publish = _uses_runtime_auto_profile(task, req, after_render)
        store = FileStore(settings)
        store.ensure_ready()

        from videoroll.apps.orchestrator_api.schemas import PublishAllRequest
        from videoroll.apps.orchestrator_api.services.publishing_service import (
            build_auto_publish_after_render,
            publish_all,
        )

        if automatic_publish:
            publish_profile = dict(get_auto_profile(db))
            if not bool(publish_profile.get("auto_publish")):
                return {"status": "skipped", "detail": "automatic publishing is disabled in the current publish box"}
            if not list(publish_profile.get("auto_publish_platforms") or []):
                return {"status": "skipped", "detail": "no automatic publish platforms are selected"}
            final_asset = (
                db.query(Asset)
                .filter(Asset.task_id == task.id, Asset.kind == AssetKind.video_final)
                .order_by(Asset.created_at.desc())
                .first()
            )
            if final_asset is None:
                return {"status": "skipped", "detail": "current render box produced no final video"}
            action = build_auto_publish_after_render(task, db=db, store=store)
            publish_payload = dict(action.get("publish_payload") or {})
        else:
            if not after_render.get("publish"):
                return {"status": "skipped"}
            publish_payload = after_render.get("publish_payload") or after_render.get("payload") or {}
            if not isinstance(publish_payload, dict):
                return {"status": "error", "detail": "after_render.publish_payload must be an object"}
            publish_payload = dict(publish_payload)

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


@celery_app.task(name="subtitle_service.auto_youtube_pipeline", bind=True, acks_late=True, reject_on_worker_lost=True)
def auto_youtube_pipeline(self: Any, task_id: str, overrides: dict[str, Any] | None = None) -> dict[str, str]:
    """
    One-click pipeline:
      - download YouTube video (+ metadata + cover)
      - generate translated subtitles
      - burn-in (and/or soft-sub) according to auto profile
      - publish to bilibili (optional, according to auto profile)
    """
    _ensure_db()
    store = FileStore(settings)
    store.ensure_ready()

    orch_base = str(settings.orchestrator_url or "").strip().rstrip("/") or "http://localhost:8000"

    db = _db()
    hb: _TaskQueueHeartbeat | None = None
    pipeline_hb: OperationHeartbeat | None = None
    pipeline_operation_key: str | None = None
    pipeline_operation_owner: str | None = None
    acquired_lock = False
    ai_usage_tokens: tuple[Any, Any] | None = None
    try:
        tid = uuid.UUID(task_id)
        pipeline_args: list[Any] = [str(tid)]
        if isinstance(overrides, dict) and overrides:
            pipeline_args.append(dict(overrides))
        task = db.get(Task, tid)
        if not task:
            raise RuntimeError("task not found")
        ai_usage_tokens = set_ai_usage_context(task_id=task.id, operation="auto_youtube")
        if task.source_type.value != "youtube":
            raise RuntimeError("task is not a youtube source")
        _raise_if_task_stopped(db, task.id)

        now = _now()
        _task_queue_lock_settings_row(db)
        cfg = get_task_queue_settings(db)
        try:
            max_conc = int(cfg.get("max_concurrency", 1))
        except Exception:
            max_conc = 1
        if max_conc < 0:
            max_conc = 0
        if max_conc == 0:
            celery_app.send_task(
                "subtitle_service.auto_youtube_pipeline",
                args=pipeline_args,
                queue="subtitle",
                countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS,
            )
            return {"status": "queued", "task_id": str(tid), "detail": "task queue paused"}

        locked_task_ids = [
            row[0]
            for row in (
                db.query(Task.id)
                .filter(Task.lock_owner == TASK_QUEUE_LOCK_OWNER, Task.lock_until.is_not(None), Task.lock_until > now)
                .order_by(Task.lock_until.asc(), Task.created_at.asc())
                .all()
            )
        ]
        has_queue_jobs = _task_has_queued_or_running_jobs(db, tid)
        if _task_queue_is_task_locked(task, now):
            # Self-heal old over-bootstrapped locks: only the first N locked tasks keep their slot.
            if (
                task.status in [TaskStatus.ingested, TaskStatus.downloaded]
                and not has_queue_jobs
                and not task_queue_slot_reserved_for(task.id, locked_task_ids, max_conc)
            ):
                _task_queue_unlock(task)
                db.add(task)
                db.commit()
                celery_app.send_task(
                    "subtitle_service.auto_youtube_pipeline",
                    args=pipeline_args,
                    queue="subtitle",
                    countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS,
                )
                return {"status": "queued", "task_id": str(tid), "detail": "waiting for task queue"}
        else:
            # Claim a task slot before doing anything heavy (download/ASR/render).
            if len(locked_task_ids) >= int(max_conc):
                celery_app.send_task(
                    "subtitle_service.auto_youtube_pipeline",
                    args=pipeline_args,
                    queue="subtitle",
                    countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS,
                )
                return {"status": "queued", "task_id": str(tid), "detail": "waiting for task queue"}

            task.lock_owner = TASK_QUEUE_LOCK_OWNER
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)
            db.commit()
            acquired_lock = True
        if task.lock_until is None or task.lock_until <= now:
            # Refresh a stale/expired lock to avoid accidental eviction mid-pipeline.
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)

        # Release the settings-row FOR UPDATE lock before YouTube download or
        # any other slow network work. Tasks dispatched by task_queue_tick
        # already own their task slot, so the branches above may otherwise
        # leave this transaction open for the full download and block every
        # later queue tick.
        db.commit()

        pipeline_meta = parse_auto_youtube_created_by(task.created_by) or {}
        pipeline_run_id = str(pipeline_meta.get("run_id") or "legacy").strip() or "legacy"
        pipeline_operation_key = f"auto-youtube-pipeline:{task.id}:{pipeline_run_id}"
        pipeline_operation_owner = f"subtitle_service.auto_youtube_pipeline:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        claim = claim_operation(
            db,
            pipeline_operation_key,
            pipeline_operation_owner,
            3600,
            request_json={"task_id": str(task.id), "run_id": pipeline_run_id},
        )
        db.commit()
        if not claim.acquired:
            if claim.result_json is not None:
                return claim.result_json
            return {"status": "in_progress", "task_id": str(tid), "detail": "automatic pipeline is already running"}

        pipeline_hb = OperationHeartbeat(
            lambda: _db(),
            pipeline_operation_key,
            pipeline_operation_owner,
            3600,
        )
        pipeline_hb.start()

        def _finish_pipeline(result: dict[str, Any]) -> dict[str, Any]:
            finish_db = _db()
            try:
                finish_operation(finish_db, pipeline_operation_key, result)
                finish_db.commit()
            finally:
                finish_db.close()
            return result

        def _release_pipeline(error: object) -> None:
            release_db = _db()
            try:
                release_operation(release_db, pipeline_operation_key, pipeline_operation_owner, error)
                release_db.commit()
            finally:
                release_db.close()

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()

        # Future boxes deliberately do not inherit this point-in-time profile.
        # It is only a local snapshot for decisions made in this pipeline stage;
        # ASR/translate/render/publish each reread the profile when they start.
        profile = dict(get_auto_profile(db))

        # Download YouTube video + cover + metadata (idempotent).
        yt: dict[str, Any] = {}
        yt_retries_done = int(getattr(self.request, "retries", 0) or 0)
        yt_max_retries = 2

        def _youtube_retry_countdown(retry_no: int) -> float:
            return min(30.0, float(3 * (2**retry_no)))

        while True:
            try:
                timeout_seconds = float(getattr(settings, "orchestrator_timeout_seconds", 1800.0) or 1800.0)
                with httpx.Client(
                    timeout=httpx.Timeout(timeout_seconds, connect=10.0),
                    headers=_orchestrator_internal_headers(),
                ) as client:
                    resp = client.post(f"{orch_base}/tasks/{tid}/actions/youtube_download")
                    resp.raise_for_status()
                    yt = resp.json() if resp.content else {}
                break
            except httpx.HTTPStatusError as e:
                status_code = int(getattr(e.response, "status_code", 0) or 0)
                if status_code in {409, 429, 500, 502, 503, 504} and yt_retries_done < yt_max_retries:
                    retry_no = yt_retries_done + 1
                    try:
                        task.retry_count = int(task.retry_count or 0) + 1
                        msg = (e.response.text or "").strip()
                        if len(msg) > 300:
                            msg = msg[:299] + "..."
                        task.error_message = f"youtube_download failed; celery retrying ({retry_no}/{yt_max_retries}): {status_code} {msg}".strip()
                        db.add(task)
                        db.commit()
                    except Exception:
                        db.rollback()
                    raise self.retry(exc=e, countdown=_youtube_retry_countdown(retry_no), max_retries=yt_max_retries)
                raise
            except httpx.HTTPError as e:
                if yt_retries_done < yt_max_retries:
                    retry_no = yt_retries_done + 1
                    try:
                        task.retry_count = int(task.retry_count or 0) + 1
                        task.error_message = f"youtube_download request failed; celery retrying ({retry_no}/{yt_max_retries}): {type(e).__name__}: {e}"
                        db.add(task)
                        db.commit()
                    except Exception:
                        db.rollback()
                    raise self.retry(exc=e, countdown=_youtube_retry_countdown(retry_no), max_retries=yt_max_retries)
                raise

        if yt_retries_done:
            try:
                task.error_message = None
                db.add(task)
                db.commit()
            except Exception:
                db.rollback()

        _raise_if_task_stopped(db, task.id)

        yt_meta = yt.get("metadata") if isinstance(yt, dict) else {}
        if not isinstance(yt_meta, dict):
            yt_meta = {}
        yt_title = str(yt_meta.get("title") or "").strip()
        yt_desc = str(yt_meta.get("description") or "")
        webpage_url = str(yt_meta.get("webpage_url") or task.source_url or "").strip()
        yt_uploader = str(yt_meta.get("uploader") or yt_meta.get("channel") or yt_meta.get("uploader_id") or "").strip()

        video_key = None
        if isinstance(yt, dict):
            va = yt.get("video_asset")
            if isinstance(va, dict):
                video_key = str(va.get("storage_key") or "").strip() or None
        if not video_key:
            latest_video = (
                db.query(Asset)
                .filter(Asset.task_id == tid, Asset.kind == AssetKind.video_raw)
                .order_by(Asset.created_at.desc())
                .first()
            )
            video_key = latest_video.storage_key if latest_video else None
        if not video_key:
            raise RuntimeError("no raw video asset found after youtube_download")

        cover_key = None
        if isinstance(yt, dict):
            ca = yt.get("cover_asset")
            if isinstance(ca, dict):
                cover_key = str(ca.get("storage_key") or "").strip() or None
        if not cover_key:
            latest_cover = (
                db.query(Asset)
                .filter(Asset.task_id == tid, Asset.kind == AssetKind.cover_image)
                .order_by(Asset.created_at.desc())
                .first()
            )
            cover_key = latest_cover.storage_key if latest_cover else None

        # Queue the next box without freezing ASR/translation/render settings.
        # The worker identifies this as an automatic task from task.created_by
        # and reads the then-current profile at each stage boundary.
        final_asset = (
            db.query(Asset)
            .filter(Asset.task_id == tid, Asset.kind == AssetKind.video_final)
            .order_by(Asset.created_at.desc())
            .first()
        )
        if not final_asset:
            req = {
                "task_id": str(tid),
                "resume": task.status == TaskStatus.failed,
                "runtime_profile": True,
                "input": {"type": "storage", "key": video_key},
                "asr": {"engine": "auto", "language": "auto", "model": None},
                "translate": {},
                "output": {"formats": ["srt"], "render": {}},
                "output_prefix": f"sub/{tid}/",
                # Always evaluate the publish box after the render boundary.
                # Its current config decides whether anything is actually submitted.
                "after_render": {"publish": True, "runtime_profile": True},
            }

            active_job = _active_pipeline_job(db, tid)
            if active_job is not None:
                existing_kind, existing_job_id = active_job
                _kick_task_queue()
                return _finish_pipeline(
                    {
                        "status": "ok",
                        "task_id": str(tid),
                        "detail": f"reused active {existing_kind} job {existing_job_id}",
                    }
                )

            job = SubtitleJob(task_id=tid, request_json=req, status=SubtitleJobStatus.queued, progress=0)
            db.add(job)
            db.commit()
            db.refresh(job)

            _kick_task_queue()
            return _finish_pipeline({"status": "ok", "task_id": str(tid), "detail": f"queued subtitle job {job.id}"})

        # A pre-existing final video can jump straight to the publish box. Read
        # only the configuration that exists when it reaches this box; legacy
        # task metadata/Celery overrides are historical snapshots, not controls.
        profile = dict(get_auto_profile(db))
        result_data: dict[str, Any] = {}
        auto_publish_platforms = list(profile.get("auto_publish_platforms") or [])
        if profile.get("auto_publish") and auto_publish_platforms:
            task = db.get(Task, tid)
            _raise_if_task_stopped(db, tid)
            if not final_asset:
                raise RuntimeError("no final video asset found; enable burn_in/soft_sub in auto profile")

            meta = default_publish_meta(db)
            translate_settings = get_translate_settings(db, settings)
            meta = apply_publish_source_overrides(
                meta,
                source_title=yt_title,
                source_description=yt_desc,
                source_url=webpage_url,
                source_uploader=yt_uploader,
                profile=profile,
                translate_settings=translate_settings,
                summary=get_task_bilibili_summary(db, str(tid)),
                ai_service=_ai_service(),
            )

            publish_meta_key = f"meta/{tid}/publish_meta.json"
            store.put_bytes(
                json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
                publish_meta_key,
                content_type="application/json",
            )

            publish_payload = {
                "account_id": None,
                "platforms": auto_publish_platforms,
                "video_key": final_asset.storage_key,
                "cover_key": cover_key,
                "typeid_mode": profile.get("publish_typeid_mode") or "ai_summary",
                "meta": None,
            }

            from videoroll.apps.orchestrator_api.schemas import PublishAllRequest
            from videoroll.apps.orchestrator_api.services.publishing_service import publish_all

            result_data = publish_all(
                tid,
                PublishAllRequest.model_validate(publish_payload),
                get_orchestrator_settings(),
                db,
                store,
            )

            # Log partial failures but don't fail the task if at least one platform succeeded.
            errors = result_data.get("errors", {}) if isinstance(result_data, dict) else {}
            if errors:
                logger.warning("auto_youtube_pipeline partial failure for task %s: %s", tid, errors)
            if not result_data.get("has_any_accepted", False) and errors:
                error_details = "; ".join(f"{p}: {msg}" for p, msg in errors.items())
                return _finish_pipeline({
                    "status": "error",
                    "task_id": str(tid),
                    "detail": f"all platforms failed: {error_details}",
                    "platforms": result_data,
                })

        return _finish_pipeline({"status": "ok", "task_id": str(tid), "platforms": result_data})
    except _TaskStopped:
        task = db.get(Task, uuid.UUID(task_id))
        if task and task.lock_owner == TASK_QUEUE_LOCK_OWNER:
            _task_queue_unlock(task)
            db.add(task)
            db.commit()
        _kick_task_queue()
        result = {"status": "stopped", "task_id": task_id, "detail": "task stopped by user"}
        if pipeline_operation_key and pipeline_operation_owner:
            return _finish_pipeline(result)
        return result
    except Retry as exc:
        if pipeline_operation_key and pipeline_operation_owner:
            _release_pipeline(exc)
        raise
    except Exception as e:
        task = db.get(Task, uuid.UUID(task_id))
        if task:
            if _task_is_stopped(db, task.id):
                _kick_task_queue()
                result = {"status": "stopped", "task_id": str(task.id), "detail": "task stopped by user"}
                if pipeline_operation_key and pipeline_operation_owner:
                    return _finish_pipeline(result)
                return result
            if task.status == TaskStatus.ready_for_review and task.error_code == "AI_REVIEW_REJECTED":
                _kick_task_queue()
                result = {"status": "review_rejected", "task_id": str(task.id), "detail": task.error_message or str(e)}
                if pipeline_operation_key and pipeline_operation_owner:
                    return _finish_pipeline(result)
                return result
            task.status = TaskStatus.failed
            task.error_message = str(e)
            db.add(task)
            db.commit()
        if pipeline_operation_key and pipeline_operation_owner:
            _release_pipeline(e)
        _kick_task_queue()
        raise
    finally:
        if pipeline_hb is not None:
            pipeline_hb.stop()
        if hb is not None:
            hb.stop()
        try:
            if acquired_lock:
                tid2 = uuid.UUID(task_id)
                task2 = db.get(Task, tid2)
                if task2 and task2.lock_owner == TASK_QUEUE_LOCK_OWNER:
                    inflight = (
                        db.query(SubtitleJob)
                        .filter(
                            SubtitleJob.task_id == tid2,
                            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
                        )
                        .count()
                        + db.query(RenderJob)
                        .filter(RenderJob.task_id == tid2, RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]))
                        .count()
                    )
                    if not inflight:
                        _task_queue_unlock(task2)
                        db.add(task2)
                        db.commit()
                        _kick_task_queue()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        reset_ai_usage_context(ai_usage_tokens)
        db.close()
