from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from celery import Celery
from celery.exceptions import Retry
from celery.signals import worker_init
from sqlalchemy import or_
from sqlalchemy.orm import Session

from videoroll.ai.client import openai_chat_config_from_settings
from videoroll.ai.service import AIService, translate_text_openai
from videoroll.config import get_orchestrator_settings, get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.auto_migrate import auto_migrate
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
    SubtitleFormat,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store
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
    render_burn_in,
    srt_to_segments,
    segments_from_json_data,
    segments_to_ass,
    segments_to_json_data,
    segments_to_srt,
    transcribe_faster_whisper,
    transcribe_mock,
    transcribe_openvino_whisper,
    translate_segments_openai_with_summary,
    translate_segments_mock,
    write_json,
)
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.subtitle_service.bilibili_tags_store import set_task_bilibili_tags
from videoroll.apps.subtitle_service.model_downloads import (
    default_model_dir_name,
    download_model_snapshot,
    resolve_model_repo_id,
)
from videoroll.apps.subtitle_service.rag import build_rag_context, rag_settings_from_translate_settings
from videoroll.apps.subtitle_service.embeddings import embedding_settings_from_translate_settings
from videoroll.apps.subtitle_service.task_title_store import set_task_titles
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.apps.publish_meta_draft import apply_publish_source_overrides, default_publish_meta
from videoroll.apps.outbox.dispatcher import dispatch_outbox_events
from videoroll.apps.outbox.service import create_outbox_event
from videoroll.apps.outbox.worker_inbox import claim_outbox_operation, finish_operation, release_operation
from videoroll.apps.publish_lifecycle import enqueue_publish_batch_cleanup, mark_publish_batch_cleanup_enqueued
from videoroll.apps.orchestrator_api.youtube_downloader import (
    download_youtube_subtitle,
    extract_youtube_metadata,
    normalize_youtube_subtitle_mode,
    pick_preferred_youtube_subtitle,
)
from videoroll.apps.subtitle_service.render_queue_store import TASK_QUEUE_SETTINGS_KEY, get_task_queue_settings
from videoroll.apps.subtitle_service.worker_concurrency import (
    JobLeaseHeartbeat,
    acquire_job_lease,
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
    beat_schedule={
        "subtitle-service-task-queue-tick": {
            "task": "subtitle_service.task_queue_tick",
            "schedule": _TASK_QUEUE_TICK_INTERVAL_SECONDS,
            "args": (),
            "options": {"queue": "subtitle"},
        },
        "subtitle-service-publish-cleanup-retry": {
            "task": "subtitle_service.enqueue_pending_publish_batch_cleanups",
            "schedule": 60.0,
            "args": (),
            "options": {"queue": "subtitle"},
        },
        "subtitle-service-outbox-dispatch": {
            "task": "subtitle_service.dispatch_outbox",
            "schedule": 5.0,
            "args": (),
            "options": {"queue": "subtitle"},
        },
        "subtitle-service-publish-dispatch-recovery": {
            "task": "subtitle_service.recover_publish_dispatches",
            "schedule": 30.0,
            "args": (),
            "options": {"queue": "subtitle"},
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
        engine = get_engine(settings.database_url)
        Base.metadata.create_all(engine)
        auto_migrate(settings.database_url)
        _DB_READY_PID = pid


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# Task Queue (task-level concurrency)
TASK_QUEUE_LOCK_OWNER = "subtitle_service.task_queue"
_TASK_QUEUE_LOCK_TTL = timedelta(seconds=300)
_TASK_QUEUE_HEARTBEAT_INTERVAL_SECONDS = 30.0
_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS = 10
_JOB_LEASE_TTL_SECONDS = 900


def _task_queue_expires_at(now: datetime) -> datetime:
    return now + _TASK_QUEUE_LOCK_TTL


def _task_queue_is_task_locked(task: Task, now: datetime) -> bool:
    return bool(task.lock_owner == TASK_QUEUE_LOCK_OWNER and task.lock_until and task.lock_until > now)


def _task_queue_unlock(task: Task) -> None:
    task.lock_owner = None
    task.lock_until = None


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
    store: S3Store,
) -> dict[str, Any] | None:
    if not profile.get("auto_publish"):
        return None

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
        "video_key": None,
        "cover_key": cover_key,
        "typeid_mode": profile.get("publish_typeid_mode") or "ai_summary",
        "meta": None,
    }
    return {"publish": True, "publish_payload": publish_payload}


def _task_queue_join_message(message: str | None, detail: str, *, limit: int = 2000) -> str:
    head = str(message or "").strip()
    tail = str(detail or "").strip()
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


def _safe_upload_log(store: S3Store, log_path: Path | None, log_key: str | None) -> None:
    if log_path is None or not log_key:
        return
    try:
        if log_path.exists():
            store.upload_file(log_path, log_key, content_type="text/plain")
    except Exception:
        pass


def _cleanup_local_work_root(path: Path | None) -> None:
    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _seed_log_from_store(store: S3Store, log_key: str, log_path: Path) -> None:
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


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uF900-\uFAFF]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _translate_title_openai(
    title: str,
    *,
    target_lang: str,
    style: str,
    translate_settings: dict[str, Any] | None = None,
    ai_service: AIService | None = None,
) -> str:
    if not title.strip():
        return title
    if ai_service is None and not (translate_settings or {}).get("openai_api_key"):
        return title
    try:
        if ai_service is not None:
            return ai_service.translate_text(title, target_lang=target_lang, style=style)
        return translate_text_openai(
            title,
            target_lang=target_lang,
            style=style,
            config=openai_chat_config_from_settings(translate_settings or {}),
        )
    except Exception:
        return title


def _read_s3_bytes(store: S3Store, key: str) -> bytes:
    obj = store.get_object(key)
    body = obj.get("Body")
    if not body:
        return b""
    try:
        return body.read() or b""
    finally:
        try:
            body.close()
        except Exception:
            pass


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

    return settings.model_copy(update={"youtube_proxy": proxy or None, "youtube_cookie_file": cookie_file})


def _download_youtube_subtitle_segments(
    *,
    task: Task,
    db: Session,
    work_root: Path,
    log_path: Path | None,
    log_key: str | None,
    store: S3Store,
    target_lang: str,
    youtube_subtitle_mode: str,
) -> tuple[list[Segment] | None, dict[str, str] | None]:
    normalized_mode = normalize_youtube_subtitle_mode(youtube_subtitle_mode)
    if task.source_type != SourceType.youtube or normalized_mode == "off":
        return None, None

    source_url = str(task.source_url or "").strip()
    if not source_url:
        return None, None

    _safe_append_log_line(log_path, f"youtube subtitles: probing mode={normalized_mode} target_lang={target_lang or 'zh'}")
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
            _safe_append_log_line(log_path, f"youtube subtitles: metadata fetched in {time.monotonic() - started_at:.1f}s")
            _safe_upload_log(store, log_path, log_key)
            selection = pick_preferred_youtube_subtitle(info, target_lang=target_lang, mode=normalized_mode)
            if selection is None:
                if normalized_mode == "auto_source":
                    _safe_append_log_line(log_path, "youtube subtitles: no auto-generated source subtitles found; fallback to ASR")
                else:
                    _safe_append_log_line(log_path, "youtube subtitles: no target-language subtitles found; fallback to ASR")
                return None, None

            _safe_append_log_line(
                log_path,
                f"youtube subtitles: downloading track source={selection.source} language={selection.language} reason={selection.reason}",
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
                _safe_append_log_line(log_path, f"youtube subtitles: converting {subtitle_path.suffix.lower() or '(unknown)'} -> srt")
                convert_subtitle_to_srt(settings.ffmpeg_path, subtitle_path, srt_input_path, log_path=log_path)

            segments = srt_to_segments(srt_input_path.read_text(encoding="utf-8"))
            if not segments:
                _safe_append_log_line(log_path, "youtube subtitles: parsed 0 segments; fallback to ASR")
                return None, None

            _safe_append_log_line(
                log_path,
                f"youtube subtitles: selected {selection.source}:{selection.language} reason={selection.reason} segments={len(segments)}",
            )
            return segments, {"language": selection.language, "source": selection.source, "reason": selection.reason}
    except Exception as e:
        _safe_append_log_line(log_path, f"youtube subtitles: probe/download failed; fallback to ASR: {type(e).__name__}: {e}")
        _safe_upload_log(store, log_path, log_key)
        return None, None


def _latest_youtube_title(db: Session, store: S3Store, task_id: uuid.UUID) -> str:
    asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not asset:
        return ""
    try:
        raw = _read_s3_bytes(store, asset.storage_key)
        info = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return ""
    if not isinstance(info, dict):
        return ""
    title = info.get("title") or info.get("fulltitle") or info.get("alt_title") or ""
    return str(title or "").strip()


def _segments_text_excerpt(segments: list[Segment], max_chars: int = 7000) -> str:
    text = "\n".join((s.text or "").strip() for s in segments if (s.text or "").strip())
    text = text.strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    half = max(1, max_chars // 2)
    return (text[:half].rstrip() + "\n…\n" + text[-half:].lstrip()).strip()


_EN_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "this",
    "that",
    "is",
    "are",
    "be",
    "as",
    "it",
    "we",
    "you",
    "i",
}


def _fallback_bilibili_tags(*, title: str, summary: str, transcript: str, n: int = 6) -> list[str]:
    blob = "\n".join([title or "", summary or "", transcript or ""])
    blob = blob.strip()
    out: list[str] = []
    seen: set[str] = set()

    def _add(tag: str) -> None:
        s = (tag or "").strip().lstrip("#").lstrip("＃")
        s = "".join(s.split())
        if not s:
            return
        if s.lower() == "videoroll":
            return
        if len(s) > 20:
            s = s[:20]
        k = s.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(s)

    # Prefer CJK chunks as tags.
    for m in re.finditer(r"[\u4e00-\u9fff]{2,12}", blob):
        _add(m.group(0))
        if len(out) >= n:
            return out[:n]

    # Then English-ish words from title/summary.
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9+-]{2,15}", blob):
        w = m.group(0)
        if w.lower() in _EN_STOP:
            continue
        _add(w)
        if len(out) >= n:
            return out[:n]

    # Generic fallback.
    for t in ["熟肉", "字幕", "翻译", "科技", "教程", "YouTube", "搬运", "科普"]:
        _add(t)
        if len(out) >= n:
            return out[:n]

    return out[:n]


@worker_init.connect
def _on_worker_init(**_kwargs: Any) -> None:
    """Initialize runtime state and let the scheduler recover expired leases."""
    # Validate production service identity during an actual worker start, not
    # while this module is imported by offline tooling or tests.
    _orchestrator_internal_headers()
    try:
        _ensure_db()
        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
    except Exception:
        logger.exception("subtitle worker initialization failed")


@celery_app.task(name="subtitle_service.process_job", bind=True, acks_late=True, reject_on_worker_lost=True)
def process_job(self: Any, job_id: str) -> dict[str, str]:
    _ensure_db()
    store = S3Store(settings)
    store.ensure_bucket()

    jid = uuid.UUID(job_id)
    db = _db()
    log_path: Path | None = None
    log_key: str | None = None
    hb: _TaskQueueHeartbeat | None = None
    job_hb: JobLeaseHeartbeat | None = None
    lease_owner: str | None = None
    work_root: Path | None = None
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

        now = _now()
        if job.status == SubtitleJobStatus.running and job.lease_until is not None and job.lease_until > now:
            return {"status": "in_progress", "detail": "job has a live worker lease"}
        if task.lock_owner != TASK_QUEUE_LOCK_OWNER:
            # Do not rewrite an existing running row here.  Only the lease
            # recovery scheduler may decide that a worker is dead.
            if job.status == SubtitleJobStatus.running:
                return {"status": "in_progress", "detail": "running job awaits lease recovery"}
            celery_app.send_task(
                "subtitle_service.task_queue_tick",
                args=[],
                queue="subtitle",
                countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS,
            )
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

        req = dict(job.request_json) if isinstance(job.request_json, dict) else {}
        input_key = (req.get("input") or {}).get("key")
        if not input_key:
            raise ValueError("missing input.key")

        work_root = Path(settings.work_dir) / "subtitle" / str(job.id)
        work_root.mkdir(parents=True, exist_ok=True)

        video_path = work_root / "input.mp4"
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
        output_cfg = (req.get("output") or {})
        formats = output_cfg.get("formats") or []
        render_cfg = output_cfg.get("render") or {}
        burn_in = bool(render_cfg.get("burn_in"))
        soft_sub = bool(render_cfg.get("soft_sub"))
        video_codec = str(render_cfg.get("video_codec") or "av1").strip().lower() or "av1"
        use_intel_gpu = bool(render_cfg.get("use_intel_gpu"))
        video_crf = render_cfg.get("video_crf")
        video_preset = render_cfg.get("video_preset")

        want_ass = "ass" in formats
        need_ass = want_ass or burn_in
        youtube_subtitle_mode = normalize_youtube_subtitle_mode(
            req.get("youtube_subtitle_mode"),
            prefer_youtube_subtitles=req.get("prefer_youtube_subtitles", True),
        )
        prefer_youtube_subtitles = youtube_subtitle_mode != "off"
        translate_cfg = req.get("translate") or {}
        translate_enabled = bool(translate_cfg.get("enabled"))
        target_lang = (translate_cfg.get("target_lang") or "zh").strip() or "zh"
        provider = (translate_cfg.get("provider") or "mock").strip() or "mock"
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

        def _download_latest_asset(kind: AssetKind, dest: Path) -> Asset | None:
            row = (
                db.query(Asset)
                .filter(Asset.task_id == task.id, Asset.kind == kind)
                .order_by(Asset.created_at.desc())
                .first()
            )
            if not row:
                return None
            try:
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

        video_downloaded = False

        def _ensure_video() -> None:
            nonlocal video_downloaded
            if video_downloaded:
                return
            store.download_file(input_key, video_path)
            video_downloaded = True

        def _ass_resolution() -> tuple[int, int]:
            try:
                _ensure_video()
                return probe_video_resolution(settings.ffmpeg_path, video_path)
            except Exception as e:
                _safe_append_log_line(log_path, f"subtitle ass resolution probe failed; fallback to 1920x1080: {e}")
                return 1920, 1080

        def _store_ass_from_segments(segs: list[Segment], *, log_prefix: str) -> str:
            play_res_x, play_res_y = _ass_resolution()
            secondary_line_scale = 0.68 if bool((req.get("translate") or {}).get("bilingual")) else None
            ass_text = segments_to_ass(
                segs,
                style_name=render_cfg.get("ass_style", "clean_white"),
                play_res_x=play_res_x,
                play_res_y=play_res_y,
                secondary_line_scale=secondary_line_scale,
                primary_font_scale_percent=render_cfg.get("primary_font_scale_percent") or 100,
                secondary_font_scale_percent=render_cfg.get("secondary_font_scale_percent") or 100,
            )
            ass_path.write_text(ass_text, encoding="utf-8")
            ass_sha = sha256_file(ass_path)
            existing_asset = (
                db.query(Asset)
                .filter(
                    Asset.task_id == task.id,
                    Asset.kind == AssetKind.subtitle_ass,
                    Asset.sha256 == ass_sha,
                )
                .first()
            )
            if existing_asset is None:
                ass_key_local = _unique_storage_key(
                    f"sub/{task.id}/subtitle_zh",
                    ass_sha,
                    ".ass",
                )
                store.upload_file(ass_path, ass_key_local, content_type="text/plain")
                db.add(
                    Asset(
                        task_id=task.id,
                        kind=AssetKind.subtitle_ass,
                        storage_key=ass_key_local,
                        sha256=ass_sha,
                        size_bytes=ass_path.stat().st_size,
                    )
                )
                existing_subtitle = (
                    db.query(Subtitle)
                    .filter(
                        Subtitle.task_id == task.id,
                        Subtitle.format == SubtitleFormat.ass,
                        Subtitle.language == "zh",
                        Subtitle.storage_key == ass_key_local,
                    )
                    .first()
                )
                if existing_subtitle is None:
                    db.add(Subtitle(task_id=task.id, version=1, format=SubtitleFormat.ass, language="zh", storage_key=ass_key_local))
                _safe_append_log_line(log_path, f"{log_prefix}: {ass_key_local} ({play_res_x}x{play_res_y})")
            else:
                ass_key_local = existing_asset.storage_key
                _safe_append_log_line(log_path, f"{log_prefix} unchanged: {ass_key_local} ({play_res_x}x{play_res_y})")
            return ass_key_local

        def _store_final_subtitle_segments(segs: list[Segment]) -> str:
            write_json(subtitle_segments_path, segments_to_json_data(segs))
            subtitle_segments_sha = sha256_file(subtitle_segments_path)
            key = _unique_storage_key(
                f"sub/{task.id}/subtitle_segments",
                subtitle_segments_sha,
                ".json",
            )
            store.upload_file(subtitle_segments_path, key, content_type="application/json")
            _set_final_subtitle_segments_key(key)
            db.commit()
            _safe_append_log_line(log_path, f"subtitle segments uploaded: {key}")
            return key

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
            task.status = TaskStatus.asr_done
            db.add(task)
            db.commit()
            _safe_append_log_line(log_path, f"{source_label}: segments={len(segs)}")
            _safe_upload_log(store, log_path, log_key)

        def _translation_checkpoint_key() -> str:
            return f"sub/{task.id}/translation_checkpoint.json"

        def _translation_checkpoint_matches(source: list[Segment], translated_prefix: list[Segment]) -> bool:
            if len(translated_prefix) > len(source):
                return False
            for i, translated_seg in enumerate(translated_prefix):
                source_seg = source[i]
                if abs(float(translated_seg.start) - float(source_seg.start)) > 0.01:
                    return False
                if abs(float(translated_seg.end) - float(source_seg.end)) > 0.01:
                    return False
            return True

        def _load_translation_checkpoint(source: list[Segment], *, source_segments_key: str | None) -> tuple[list[Segment], str]:
            if not source or not source_segments_key:
                return [], ""
            try:
                store.download_file(_translation_checkpoint_key(), translation_checkpoint_path)
                payload = json.loads(translation_checkpoint_path.read_text(encoding="utf-8"))
            except Exception:
                return [], ""
            if not isinstance(payload, dict):
                return [], ""
            if str(payload.get("source_segments_key") or "").strip() != str(source_segments_key or "").strip():
                return [], ""
            translated_prefix = segments_from_json_data(payload.get("translated_segments"))
            if not translated_prefix:
                return [], ""
            if not _translation_checkpoint_matches(source, translated_prefix):
                return [], ""
            summary = str(payload.get("summary") or "").strip()[:500]
            return translated_prefix, summary

        def _save_translation_checkpoint(source_segments_key: str | None, translated_prefix: list[Segment], *, summary: str) -> None:
            if not source_segments_key or not translated_prefix:
                return
            payload = {
                "source_segments_key": str(source_segments_key or "").strip(),
                "summary": str(summary or "").strip()[:500],
                "translated_segments": segments_to_json_data(translated_prefix),
            }
            try:
                write_json(translation_checkpoint_path, payload)
                store.upload_file(translation_checkpoint_path, _translation_checkpoint_key(), content_type="application/json")
            except Exception as e:
                _safe_append_log_line(log_path, f"translation checkpoint save failed: {type(e).__name__}: {e}")

        def _clear_translation_checkpoint() -> None:
            try:
                store.delete_object(_translation_checkpoint_key())
            except Exception:
                pass

        if not resume:
            _clear_translation_checkpoint()

        srt_asset = _download_latest_asset(AssetKind.subtitle_srt, srt_path) if resume else None
        if resume and srt_asset:
            srt_key = srt_asset.storage_key
            _clear_translation_checkpoint()
            _safe_append_log_line(log_path, f"resume: found existing subtitle_srt asset: {srt_key}")
            _safe_upload_log(store, log_path, log_key)
            if task.status in {TaskStatus.failed, TaskStatus.created, TaskStatus.ingested, TaskStatus.downloaded, TaskStatus.audio_extracted, TaskStatus.asr_done, TaskStatus.translated}:
                task.status = TaskStatus.subtitle_ready
                db.add(task)
            job.progress = 80
            db.add(job)
            db.commit()

            if need_ass:
                segs = _download_final_subtitle_segments()
                if segs:
                    ass_key = _store_ass_from_segments(segs, log_prefix="generated ass from resumed subtitle segments")
                else:
                    segs = srt_to_segments(srt_path.read_text(encoding="utf-8"))
                    ass_key = _store_ass_from_segments(segs, log_prefix="generated ass from resumed srt (fallback)")
                    _safe_append_log_line(log_path, "resume: final subtitle segments not found; ass regenerated from srt without structured bilingual data")
                db.commit()
                _safe_upload_log(store, log_path, log_key)

            if not burn_in and not soft_sub:
                job.status = SubtitleJobStatus.succeeded
                job.progress = 100
                _task_queue_unlock(task)
                db.add(task)
                db.add(job)
                db.commit()
                _safe_append_log_line(log_path, "subtitle job done (no render configured)")
                _safe_upload_log(store, log_path, log_key)
                celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
                return {"status": "ok"}

            after_render = req.get("after_render") if isinstance(req, dict) else None
            if not isinstance(after_render, dict):
                after_render = None

            existing = (
                db.query(RenderJob)
                .filter(
                    RenderJob.subtitle_job_id == job.id,
                    RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
                )
                .order_by(RenderJob.created_at.desc())
                .first()
            )
            if not existing:
                payload: dict[str, Any] = {
                    "input_key": input_key,
                    "srt_key": srt_key,
                    "ass_key": ass_key if burn_in else None,
                    "burn_in": bool(burn_in),
                    "soft_sub": bool(soft_sub),
                    "render": {
                        "video_codec": video_codec,
                        "use_intel_gpu": use_intel_gpu,
                        "video_preset": video_preset,
                        "video_crf": video_crf,
                    },
                }
                if after_render:
                    payload["after_render"] = after_render
                db.add(RenderJob(task_id=task.id, subtitle_job_id=job.id, status=RenderJobStatus.queued, progress=0, request_json=payload))
                db.commit()

            job.status = SubtitleJobStatus.queued
            db.add(job)
            db.commit()
            _safe_append_log_line(log_path, "render queued; waiting for task queue")
            _safe_upload_log(store, log_path, log_key)
            celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
            return {"status": "ok", "detail": "render queued"}

        segments: list[Segment] | None = None
        segments_asset = _download_latest_asset(AssetKind.segments_json, segments_path) if resume else None
        if resume and segments_asset:
            segments_key = segments_asset.storage_key
            segments = _load_segments_json(segments_path)

        youtube_subtitle_info: dict[str, str] | None = None
        if segments is None:
            job.progress = 25
            db.add(job)
            db.commit()

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
            audio_asset = _download_latest_asset(AssetKind.audio_wav, audio_path) if resume else None
            if resume and audio_asset:
                audio_key = audio_asset.storage_key
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
                store.upload_file(audio_path, audio_key, content_type="audio/wav")
                db.add(
                    Asset(
                        task_id=task.id,
                        kind=AssetKind.audio_wav,
                        storage_key=audio_key,
                        sha256=audio_sha,
                        size_bytes=audio_path.stat().st_size,
                    )
                )
                task.status = TaskStatus.audio_extracted
                db.add(task)
                db.commit()

            asr_cfg = req.get("asr") or {}
            asr_defaults = get_asr_settings(db, settings)
            requested_engine = (asr_cfg.get("engine") or "auto").strip()
            requested_language = (asr_cfg.get("language") or "auto").strip()
            requested_model = (asr_cfg.get("model") or "").strip() or None

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
                cpu_threads_cfg = int(getattr(settings, "whisper_cpu_threads", 0) or 0)
                if cpu_threads_cfg <= 0:
                    cpu_threads_cfg = process_cpu_count() or 4
                num_workers_cfg = int(getattr(settings, "whisper_num_workers", 1) or 1)
                if num_workers_cfg <= 0:
                    num_workers_cfg = 1
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
                _safe_append_log_line(
                    log_path,
                    "asr: "
                    f"engine=openvino model={model_name} language={language} "
                    f"device={openvino_device} num_beams={openvino_num_beams} max_new_tokens={openvino_max_new_tokens}",
                )
                segments = transcribe_openvino_whisper(
                    audio_path,
                    model_name=model_name,
                    language=language,
                    device=openvino_device,
                    num_beams=openvino_num_beams,
                    max_new_tokens=openvino_max_new_tokens,
                )
            else:
                raise ValueError(f"unsupported ASR engine: {engine}")
            _store_source_segments(segments, source_label="asr done")

        job.progress = 60
        db.add(job)
        db.commit()

        translate_cfg = req.get("translate") or {}
        translate_enabled = bool(translate_cfg.get("enabled"))
        segments_out = segments
        translation_summary = ""
        if translate_enabled:
            _safe_append_log_line(log_path, f"translate: provider={provider} target_lang={target_lang} bilingual={bilingual}")
            ai_service = _ai_service()
            translate_settings = _fresh_translate_settings()
            style = (translate_cfg.get("style") or translate_settings["default_style"]).strip() or translate_settings["default_style"]
            batch_size = int(translate_cfg.get("batch_size") or translate_settings["default_batch_size"])
            enable_summary_val = translate_cfg.get("enable_summary")
            enable_summary = translate_settings["default_enable_summary"] if enable_summary_val is None else bool(enable_summary_val)

            max_retries = max(0, int(translate_settings.get("default_max_retries") or 0))

            def _is_retryable_translate_error(err: Exception) -> bool:
                msg = str(err or "")
                if "api key is not set" in msg.lower():
                    return False
                return True

            def _translate_retry_countdown(attempt: int) -> float:
                # Exponential backoff: 2s, 4s, 8s... capped at 30s.
                return min(30.0, float(2 ** max(0, attempt)))

            while True:
                try:
                    if provider == "mock":
                        segments_out = translate_segments_mock(segments, target_lang=target_lang)
                    elif provider in {"noop", "none"}:
                        segments_out = segments
                    elif provider == "openai":
                        resume_prefix, resume_summary = _load_translation_checkpoint(segments, source_segments_key=segments_key)
                        if resume_prefix:
                            _safe_append_log_line(
                                log_path,
                                f"translate: resuming from checkpoint at segment {len(resume_prefix)}/{len(segments)}",
                            )

                        checkpoint_segments = list(resume_prefix)
                        rag_settings = rag_settings_from_translate_settings(translate_settings)

                        if rag_settings.enabled:
                            _safe_append_log_line(
                                log_path,
                                "translate rag: "
                                f"enabled top_k={rag_settings.top_k} min_score={rag_settings.min_score} "
                                f"embedding_provider={rag_settings.embedding_provider} embedding_model={rag_settings.embedding_model} "
                                f"domain={rag_settings.domain or '(any)'}",
                            )

                        def _rag_context_provider(batch_segments: list[Segment], start_idx: int, summary: str) -> dict[str, Any] | None:
                            del start_idx
                            current_translate_settings = _fresh_translate_settings()
                            rag_settings = rag_settings_from_translate_settings(current_translate_settings)
                            if not rag_settings.enabled:
                                return None
                            rag_chat_config = openai_chat_config_from_settings(current_translate_settings)
                            rag_embedding_settings = embedding_settings_from_translate_settings(current_translate_settings)
                            ctx = build_rag_context(
                                db,
                                segments=batch_segments,
                                target_lang=target_lang,
                                rag_settings=rag_settings,
                                embedding_settings=rag_embedding_settings,
                                chat_config=rag_chat_config,
                                previous_summary=summary,
                                session_factory=lambda: get_sessionmaker(settings.database_url)(),
                                task_id=str(task.id),
                                subtitle_job_id=str(job.id),
                            )
                            if ctx.hits:
                                try:
                                    db.commit()
                                except Exception:
                                    db.rollback()
                            if not ctx.term_cards and not ctx.knowledge_cards:
                                return None
                            return {
                                "term_cards": ctx.term_cards,
                                "knowledge_cards": ctx.knowledge_cards,
                            }

                        def _on_translate_batch_done(batch_segments: list[Segment], updated_summary: str, completed_count: int) -> None:
                            del completed_count
                            checkpoint_segments.extend(batch_segments)
                            _save_translation_checkpoint(segments_key, checkpoint_segments, summary=updated_summary)

                        segments_out, translation_summary = translate_segments_openai_with_summary(
                            segments,
                            target_lang=target_lang,
                            style=style,
                            api_key=None,
                            base_url="",
                            model="",
                            temperature=translate_settings["openai_temperature"],
                            timeout_seconds=translate_settings["openai_timeout_seconds"],
                            batch_size=batch_size,
                            enable_summary=enable_summary,
                            rag_context_provider=_rag_context_provider,
                            resume_from=resume_prefix,
                            initial_summary=resume_summary,
                            on_batch_done=_on_translate_batch_done,
                            ai_service=ai_service,
                        )
                    else:
                        raise ValueError(f"unsupported translate provider: {provider}")
                    job.error_message = None
                    db.add(job)
                    db.commit()
                    break
                except Exception as e:
                    retry_no = int(getattr(self.request, "retries", 0) or 0) + 1
                    if provider != "openai" or retry_no > max_retries or not _is_retryable_translate_error(e):
                        raise
                    req["resume"] = True
                    _save_job_request()
                    job.error_message = f"translate failed; celery retrying ({retry_no}/{max_retries}): {e}"
                    db.add(job)
                    db.commit()
                    _safe_append_log_line(log_path, f"translate retry {retry_no}/{max_retries}: {type(e).__name__}: {e}")
                    _safe_upload_log(store, log_path, log_key)
                    raise self.retry(exc=e, countdown=_translate_retry_countdown(retry_no), max_retries=max_retries)
            task.status = TaskStatus.translated
            db.add(task)

            # Best-effort: store translated title for UI display / downloads.
            try:
                title_src = _latest_youtube_title(db, store, task.id)
                if title_src:
                    title_out = title_src
                    if provider == "openai" and not _has_cjk(title_src):
                        try:
                            title_out = _translate_title_openai(
                                title_src,
                                target_lang=target_lang,
                                style=style,
                                ai_service=ai_service,
                            )
                        except Exception:
                            title_out = title_src
                    set_task_titles(db, str(task.id), source_title=title_src, translated_title=title_out)
            except Exception:
                pass

            # Best-effort: generate Bilibili tags from translation summary + transcript excerpt.
            try:
                title_hint = _latest_youtube_title(db, store, task.id)
                transcript_excerpt = _segments_text_excerpt(segments_out, max_chars=7000)
                tags: list[str] = []
                if provider == "openai":
                    try:
                        tags = ai_service.generate_bilibili_tags(
                            title=title_hint,
                            summary=translation_summary,
                            transcript=transcript_excerpt,
                            n_tags=6,
                        )
                    except Exception:
                        tags = []
                if len(tags) < 6:
                    tags = _fallback_bilibili_tags(title=title_hint, summary=translation_summary, transcript=transcript_excerpt, n=6)
                if tags:
                    set_task_bilibili_tags(db, str(task.id), tags=tags[:6], title=title_hint, summary=translation_summary)
            except Exception:
                pass

            if bilingual:
                merged: list[Segment] = []
                for src_seg, zh_seg in zip(segments, segments_out):
                    merged.append(
                        Segment(
                            start=zh_seg.start,
                            end=zh_seg.end,
                            text=zh_seg.text,
                            confidence=zh_seg.confidence,
                            secondary_text=str(src_seg.text or "").strip() or None,
                        )
                    )
                segments_out = merged

        srt_text = segments_to_srt(segments_out)
        srt_path.write_text(srt_text, encoding="utf-8")
        _store_final_subtitle_segments(segments_out)
        srt_sha = sha256_file(srt_path)
        srt_key = _unique_storage_key(
            f"sub/{task.id}/subtitle_zh",
            srt_sha,
            ".srt",
        )
        store.upload_file(srt_path, srt_key, content_type="text/plain")
        _clear_translation_checkpoint()
        db.add(
            Asset(
                task_id=task.id,
                kind=AssetKind.subtitle_srt,
                storage_key=srt_key,
                sha256=srt_sha,
                size_bytes=srt_path.stat().st_size,
            )
        )
        db.add(Subtitle(task_id=task.id, version=1, format=SubtitleFormat.srt, language="zh", storage_key=srt_key))
        _safe_append_log_line(log_path, f"subtitle srt uploaded: {srt_key}")

        if need_ass:
            ass_key = _store_ass_from_segments(segments_out, log_prefix="subtitle ass uploaded")

        task.status = TaskStatus.subtitle_ready
        db.add(task)
        job.progress = 80
        db.add(job)
        db.commit()
        _safe_upload_log(store, log_path, log_key)

        if not burn_in and not soft_sub:
            job.status = SubtitleJobStatus.succeeded
            job.progress = 100
            _task_queue_unlock(task)
            db.add(task)
            db.add(job)
            db.commit()
            _safe_append_log_line(log_path, "subtitle job done (no render configured)")
            _safe_upload_log(store, log_path, log_key)
            celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
            return {"status": "ok"}

        after_render = req.get("after_render") if isinstance(req, dict) else None
        if not isinstance(after_render, dict):
            after_render = None

        existing = (
            db.query(RenderJob)
            .filter(
                RenderJob.subtitle_job_id == job.id,
                RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
            )
            .order_by(RenderJob.created_at.desc())
            .first()
        )
        if not existing:
            payload: dict[str, Any] = {
                "input_key": input_key,
                "srt_key": srt_key,
                "ass_key": ass_key if burn_in else None,
                "burn_in": bool(burn_in),
                "soft_sub": bool(soft_sub),
                "render": {
                    "video_codec": video_codec,
                    "use_intel_gpu": use_intel_gpu,
                    "video_preset": video_preset,
                    "video_crf": video_crf,
                },
            }
            if after_render:
                payload["after_render"] = after_render
            db.add(RenderJob(task_id=task.id, subtitle_job_id=job.id, status=RenderJobStatus.queued, progress=0, request_json=payload))
            db.commit()

        job.status = SubtitleJobStatus.queued
        db.add(job)
        db.commit()
        _safe_append_log_line(log_path, "render queued; waiting for task queue")
        _safe_upload_log(store, log_path, log_key)
        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
        return {"status": "ok", "detail": "render queued"}
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
            if task and task.status not in {TaskStatus.published, TaskStatus.canceled}:
                task.status = TaskStatus.failed
                task.error_code = task.error_code or "SUBTITLE_FAILED"
                task.error_message = str(e)
                db.add(task)
        db.commit()
        _safe_append_log_line(log_path, f"ERROR: {type(e).__name__}: {e}")
        _safe_append_log_block(log_path, traceback.format_exc())
        _safe_upload_log(store, log_path, log_key)
        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
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

        unlocked = or_(
            Task.lock_owner != TASK_QUEUE_LOCK_OWNER,
            Task.lock_until.is_(None),
            Task.lock_until <= now,
        )

        locked_tasks = (
            db.query(Task)
            .filter(Task.lock_owner == TASK_QUEUE_LOCK_OWNER, Task.lock_until.is_not(None), Task.lock_until > now)
            .order_by(Task.lock_until.asc())
            .all()
        )

        # Phase 1: advance locked tasks (start their next queued job if nothing is running).
        for t in locked_tasks:
            tid = t.id
            has_running = (
                db.query(SubtitleJob).filter(SubtitleJob.task_id == tid, SubtitleJob.status == SubtitleJobStatus.running).count()
                + db.query(RenderJob).filter(RenderJob.task_id == tid, RenderJob.status == RenderJobStatus.running).count()
            )
            if has_running:
                continue

            rj = (
                db.query(RenderJob)
                .filter(RenderJob.task_id == tid, RenderJob.status == RenderJobStatus.queued)
                .order_by(RenderJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if rj:
                to_start.append(("render", str(rj.id)))
                started_render += 1
                continue

            sj = (
                db.query(SubtitleJob)
                .filter(SubtitleJob.task_id == tid, SubtitleJob.status == SubtitleJobStatus.queued)
                .order_by(SubtitleJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if sj:
                to_start.append(("subtitle", str(sj.id)))
                started_subtitle += 1

        # Phase 2: lock and start new tasks up to max_concurrency.
        running_tasks = len(locked_tasks)
        capacity = available_task_queue_capacity(max_conc, running_tasks)
        for _ in range(capacity):
            # Prefer queued render jobs (rare, usually after an expired lock) so tasks can finish.
            rj = (
                db.query(RenderJob)
                .join(Task, Task.id == RenderJob.task_id)
                .filter(
                    RenderJob.status == RenderJobStatus.queued,
                    or_(
                        Task.lock_owner != TASK_QUEUE_LOCK_OWNER,
                        Task.lock_until.is_(None),
                        Task.lock_until <= now,
                    ),
                )
                .order_by(RenderJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if rj:
                task = db.query(Task).filter(Task.id == rj.task_id).with_for_update(skip_locked=True).first()
                if not task:
                    continue
                if _task_queue_is_task_locked(task, now):
                    continue
                if task.lock_until and task.lock_until > now and task.lock_owner and task.lock_owner != TASK_QUEUE_LOCK_OWNER:
                    continue
                task.lock_owner = TASK_QUEUE_LOCK_OWNER
                task.lock_until = _task_queue_expires_at(now)
                db.add(task)
                to_start.append(("render", str(rj.id)))
                started_render += 1
                running_tasks += 1
                continue

            sj = (
                db.query(SubtitleJob)
                .join(Task, Task.id == SubtitleJob.task_id)
                .filter(
                    SubtitleJob.status == SubtitleJobStatus.queued,
                    or_(
                        Task.lock_owner != TASK_QUEUE_LOCK_OWNER,
                        Task.lock_until.is_(None),
                        Task.lock_until <= now,
                    ),
                )
                .order_by(SubtitleJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if not sj:
                break

            task = db.query(Task).filter(Task.id == sj.task_id).with_for_update(skip_locked=True).first()
            if not task:
                continue
            if _task_queue_is_task_locked(task, now):
                continue
            if task.lock_until and task.lock_until > now and task.lock_owner and task.lock_owner != TASK_QUEUE_LOCK_OWNER:
                continue

            task.lock_owner = TASK_QUEUE_LOCK_OWNER
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)
            to_start.append(("subtitle", str(sj.id)))
            started_subtitle += 1
            running_tasks += 1

        # Phase 3: bootstrap auto-YouTube tasks only with remaining task slots.
        bootstrap_capacity = available_task_queue_capacity(max_conc, running_tasks)
        if bootstrap_capacity > 0:
            bootstrap_cutoff = now - timedelta(seconds=60)
            bootstrap_candidates = (
                db.query(Task)
                .filter(
                    Task.source_type == SourceType.youtube,
                    Task.status.in_([TaskStatus.ingested, TaskStatus.downloaded]),
                    unlocked,
                    Task.updated_at.is_not(None),
                    Task.updated_at < bootstrap_cutoff,
                )
                .order_by(Task.updated_at.asc(), Task.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(max(bootstrap_capacity * 4, bootstrap_capacity))
                .all()
            )
            for task in bootstrap_candidates:
                if available_task_queue_capacity(max_conc, running_tasks) <= 0:
                    break
                meta = parse_auto_youtube_created_by(task.created_by)
                if meta is None:
                    continue
                if _task_has_queued_or_running_jobs(db, task.id):
                    continue
                if _task_queue_is_task_locked(task, now):
                    continue

                task.lock_owner = TASK_QUEUE_LOCK_OWNER
                task.lock_until = _task_queue_expires_at(now)
                db.add(task)

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

    for kind, jid in to_start:
        if kind == "subtitle":
            celery_app.send_task("subtitle_service.process_job", args=[jid], queue="subtitle")
        else:
            celery_app.send_task("subtitle_service.process_render_job", args=[jid], queue="subtitle")
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


@celery_app.task(name="subtitle_service.render_queue_tick")
def render_queue_tick() -> dict[str, Any]:
    # Legacy alias.
    return task_queue_tick()


@celery_app.task(name="subtitle_service.process_render_job", bind=True, acks_late=True, reject_on_worker_lost=True)
def process_render_job(self: Any, render_job_id: str) -> dict[str, Any]:
    _ensure_db()
    store = S3Store(settings)
    store.ensure_bucket()

    rid = uuid.UUID(render_job_id)
    db = _db()
    log_path: Path | None = None
    log_key: str | None = None
    hb: _TaskQueueHeartbeat | None = None
    job_hb: JobLeaseHeartbeat | None = None
    lease_owner: str | None = None
    work_root: Path | None = None
    try:
        rj = db.get(RenderJob, rid)
        if not rj:
            return {"status": "error", "detail": "render job not found"}

        if rj.status == RenderJobStatus.succeeded:
            return {"status": "ok", "detail": "already succeeded"}
        if rj.status == RenderJobStatus.canceled:
            return {"status": "skipped", "detail": "canceled"}

        task = db.get(Task, rj.task_id)
        if not task:
            return {"status": "error", "detail": "task not found"}

        now = _now()
        if rj.status == RenderJobStatus.running and rj.lease_until is not None and rj.lease_until > now:
            return {"status": "in_progress", "detail": "render job has a live worker lease"}
        if task.lock_owner != TASK_QUEUE_LOCK_OWNER:
            # As with subtitle work, only expired leases may move a running
            # render back to queued.
            if rj.status == RenderJobStatus.running:
                return {"status": "in_progress", "detail": "running render awaits lease recovery"}
            celery_app.send_task(
                "subtitle_service.task_queue_tick",
                args=[],
                queue="subtitle",
                countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS,
            )
            return {"status": "queued", "detail": "waiting for task queue"}
        if task.lock_until is None or task.lock_until <= now:
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)
            db.commit()

        # Best-effort: if called directly, atomically claim it before render work.
        if rj.status == RenderJobStatus.queued:
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
        candidate_owner = f"subtitle_service.process_render_job:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        if not acquire_job_lease(db, rj, candidate_owner, _JOB_LEASE_TTL_SECONDS):
            db.rollback()
            return {"status": "in_progress", "detail": "render job lease is held by another worker"}
        db.commit()
        lease_owner = candidate_owner

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()
        job_hb = JobLeaseHeartbeat(lambda: _db(), rj.id, lease_owner, _JOB_LEASE_TTL_SECONDS)
        job_hb.start()

        req = rj.request_json if isinstance(rj.request_json, dict) else {}
        input_key = str(req.get("input_key") or "").strip()
        srt_key = str(req.get("srt_key") or "").strip()
        ass_key = str(req.get("ass_key") or "").strip() or None
        burn_in = bool(req.get("burn_in"))
        soft_sub = bool(req.get("soft_sub"))
        render_cfg = req.get("render") if isinstance(req.get("render"), dict) else {}

        video_codec = str(render_cfg.get("video_codec") or "av1").strip().lower() or "av1"
        use_intel_gpu = bool(render_cfg.get("use_intel_gpu"))
        video_preset = render_cfg.get("video_preset")
        video_crf = render_cfg.get("video_crf")

        if not input_key:
            raise ValueError("render job missing input_key")
        if not srt_key:
            raise ValueError("render job missing srt_key")
        if burn_in and not ass_key:
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
            f"render job start: render_job_id={rj.id} task_id={task.id} burn_in={burn_in} soft_sub={soft_sub} codec={video_codec} intel_gpu={use_intel_gpu} preset={video_preset} crf={video_crf}",
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

        video_path = work_root / "input.mp4"
        srt_path = work_root / "subtitle_zh.srt"
        ass_path = work_root / "subtitle_zh.ass"

        _safe_append_log_line(log_path, f"download: input_key={input_key}")
        _safe_upload_log(store, log_path, log_key)
        rj.progress = max(int(rj.progress or 0), 5)
        db.add(rj)
        db.commit()
        store.download_file(input_key, video_path)
        _safe_append_log_line(log_path, f"download: srt_key={srt_key}")
        _safe_upload_log(store, log_path, log_key)
        store.download_file(srt_key, srt_path)
        if burn_in and ass_key:
            _safe_append_log_line(log_path, f"download: ass_key={ass_key}")
            _safe_upload_log(store, log_path, log_key)
            store.download_file(ass_key, ass_path)
        _safe_append_log_line(log_path, "inputs downloaded")
        _safe_upload_log(store, log_path, log_key)

        subtitle_job: SubtitleJob | None = None
        if rj.subtitle_job_id:
            subtitle_job = db.get(SubtitleJob, rj.subtitle_job_id)
            if subtitle_job:
                subtitle_job.progress = max(int(subtitle_job.progress or 0), 81)
                db.add(subtitle_job)

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
                crf=video_crf,
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
            store.upload_file(out_video, final_key, content_type="video/mp4")
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.video_final,
                    storage_key=final_key,
                    sha256=final_sha,
                    size_bytes=out_video.stat().st_size,
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
            store.upload_file(out_video, final_key, content_type="video/x-matroska")
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.video_final,
                    storage_key=final_key,
                    sha256=final_sha,
                    size_bytes=out_video.stat().st_size,
                )
            )

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
        if isinstance(after_render, dict) and after_render.get("publish"):
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
        _safe_append_log_line(log_path, "render job done")
        _safe_upload_log(store, log_path, log_key)

        if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
            _task_queue_unlock(task)
            db.add(task)
            db.commit()

        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
        return {"status": "ok"}
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
                if task.status not in {TaskStatus.published, TaskStatus.canceled}:
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
        _safe_append_log_line(log_path, f"ERROR: {type(e).__name__}: {e}")
        _safe_append_log_block(log_path, traceback.format_exc())
        _safe_upload_log(store, log_path, log_key)
        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
        return {"status": "error", "detail": str(e)}
    finally:
        if job_hb is not None:
            job_hb.stop()
        if hb is not None:
            hb.stop()
        if lease_owner is not None:
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
        if not after_render.get("publish"):
            return {"status": "skipped"}

        publish_payload = after_render.get("publish_payload") or after_render.get("payload") or {}
        if not isinstance(publish_payload, dict):
            return {"status": "error", "detail": "after_render.publish_payload must be an object"}
        publish_payload = dict(publish_payload)

        # Ensure we don't accidentally override the latest rendered asset selection.
        if publish_payload.get("video_key") in {"", None}:
            publish_payload["video_key"] = None

        from videoroll.apps.orchestrator_api.schemas import PublishAllRequest
        from videoroll.apps.orchestrator_api.services.publishing_service import publish_all

        result_data = publish_all(
            task.id,
            PublishAllRequest.model_validate(publish_payload),
            get_orchestrator_settings(),
            db,
            S3Store(settings),
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
      - delete non-final S3 assets to prevent storage from growing forever
    Keeps:
      - AssetKind.video_final
      - AssetKind.publish_result
    """
    _ensure_db()
    store = S3Store(settings)
    store.ensure_bucket()

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
            if task.active_publish_batch_id != batch.id:
                return {"status": "skipped", "detail": "publish batch is no longer active for task"}
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
            if not task or task.active_publish_batch_id != batch.id:
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
    store = S3Store(settings)
    store.ensure_bucket()

    orch_base = str(settings.orchestrator_url or "").strip().rstrip("/") or "http://localhost:8000"

    db = _db()
    hb: _TaskQueueHeartbeat | None = None
    acquired_lock = False
    try:
        tid = uuid.UUID(task_id)
        pipeline_args: list[Any] = [str(tid)]
        if isinstance(overrides, dict) and overrides:
            pipeline_args.append(dict(overrides))
        task = db.get(Task, tid)
        if not task:
            raise RuntimeError("task not found")
        if task.source_type.value != "youtube":
            raise RuntimeError("task is not a youtube source")

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
            db.commit()

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()

        profile = dict(get_auto_profile(db))
        if isinstance(overrides, dict) and overrides.get("auto_publish") is not None:
            profile["auto_publish"] = bool(overrides.get("auto_publish"))

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
                if status_code in {429, 500, 502, 503, 504} and yt_retries_done < yt_max_retries:
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
        if not cover_key and profile.get("publish_use_youtube_cover"):
            latest_cover = (
                db.query(Asset)
                .filter(Asset.task_id == tid, Asset.kind == AssetKind.cover_image)
                .order_by(Asset.created_at.desc())
                .first()
            )
            cover_key = latest_cover.storage_key if latest_cover else None

        # Render subtitles (skip if a final video already exists).
        final_asset = (
            db.query(Asset)
            .filter(Asset.task_id == tid, Asset.kind == AssetKind.video_final)
            .order_by(Asset.created_at.desc())
            .first()
        )
        if not final_asset and (profile.get("burn_in") or profile.get("soft_sub")):
            req = {
                "task_id": str(tid),
                "resume": task.status == TaskStatus.failed,
                "prefer_youtube_subtitles": bool(profile.get("prefer_youtube_subtitles", True)),
                "youtube_subtitle_mode": normalize_youtube_subtitle_mode(
                    profile.get("youtube_subtitle_mode"),
                    prefer_youtube_subtitles=profile.get("prefer_youtube_subtitles", True),
                ),
                "input": {"type": "s3", "key": video_key},
                "asr": {
                    "engine": profile.get("asr_engine") or "auto",
                    "language": profile.get("asr_language") or "auto",
                    "model": profile.get("asr_model"),
                },
                "translate": {
                    "enabled": bool(profile.get("translate_enabled")),
                    "target_lang": profile.get("target_lang") or "zh",
                    "provider": profile.get("translate_provider") or "openai",
                    "style": profile.get("translate_style") or "口语自然",
                    "enable_summary": bool(profile.get("translate_enable_summary")),
                    "bilingual": bool(profile.get("bilingual")),
                },
                "output": {
                    "formats": profile.get("formats") or ["srt", "ass"],
                    "render": {
                        "burn_in": bool(profile.get("burn_in")),
                        "soft_sub": bool(profile.get("soft_sub")),
                        "ass_style": profile.get("ass_style") or "clean_white",
                        "video_codec": profile.get("video_codec") or "av1",
                        "use_intel_gpu": bool(profile.get("use_intel_gpu")),
                        "video_preset": profile.get("video_preset"),
                        "video_crf": profile.get("video_crf"),
                        "primary_font_scale_percent": profile.get("primary_font_scale_percent") or 100,
                        "secondary_font_scale_percent": profile.get("secondary_font_scale_percent") or 100,
                    },
                },
                "output_prefix": f"sub/{tid}/",
            }

            after_render = _build_after_render_publish_action(
                task_id=tid,
                cover_key=cover_key,
                profile=profile,
                yt_title=yt_title,
                yt_desc=yt_desc,
                webpage_url=webpage_url,
                yt_uploader=yt_uploader,
                db=db,
                store=store,
            )
            if after_render:
                req["after_render"] = after_render

            job = SubtitleJob(task_id=tid, request_json=req, status=SubtitleJobStatus.queued, progress=0)
            db.add(job)
            db.commit()
            db.refresh(job)

            celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
            return {"status": "ok", "task_id": str(tid), "detail": f"queued subtitle job {job.id}"}

        result_data: dict[str, Any] = {}
        if profile.get("auto_publish"):
            task = db.get(Task, tid)
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
                return {
                    "status": "error",
                    "task_id": str(tid),
                    "detail": f"all platforms failed: {error_details}",
                    "platforms": result_data,
                }

        return {"status": "ok", "task_id": str(tid), "platforms": result_data}
    except Retry:
        raise
    except Exception as e:
        task = db.get(Task, uuid.UUID(task_id))
        if task:
            if task.status == TaskStatus.ready_for_review and task.error_code == "AI_REVIEW_REJECTED":
                celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
                return {"status": "review_rejected", "task_id": str(task.id), "detail": task.error_message or str(e)}
            task.status = TaskStatus.failed
            task.error_message = str(e)
            db.add(task)
            db.commit()
        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
        raise
    finally:
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
                        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        db.close()
