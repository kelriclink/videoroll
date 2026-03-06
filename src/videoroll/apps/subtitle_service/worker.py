from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from celery import Celery
from celery.signals import worker_init
from sqlalchemy import or_
from sqlalchemy.orm import Session

from videoroll.config import get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.auto_migrate import auto_migrate
from videoroll.db.models import (
    AppSetting,
    Asset,
    AssetKind,
    RenderJob,
    RenderJobStatus,
    Subtitle,
    SubtitleFormat,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.utils.internal_api_token import internal_api_token
from videoroll.utils.hashing import sha256_file
from videoroll.apps.orchestrator_api.admin_auth_store import INTERNAL_TOKEN_HEADER
from videoroll.apps.subtitle_service.processing import (
    Segment,
    extract_audio,
    generate_bilibili_tags_openai,
    mux_soft_sub,
    render_burn_in,
    srt_to_segments,
    segments_to_ass,
    segments_to_srt,
    transcribe_faster_whisper,
    transcribe_mock,
    translate_segments_openai,
    translate_segments_openai_with_summary,
    translate_segments_mock,
    write_json,
)
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.subtitle_service.bilibili_tags_store import set_task_bilibili_tags
from videoroll.apps.subtitle_service.task_title_store import set_task_titles
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.apps.bilibili_publisher.publish_settings_store import get_bilibili_publish_settings
from videoroll.apps.subtitle_service.render_queue_store import TASK_QUEUE_SETTINGS_KEY, get_task_queue_settings
from videoroll.utils.cpu import process_cpu_count
from videoroll.utils.hf_hub import configure_hf_hub_proxy


settings = get_subtitle_settings()
_ORCH_INTERNAL_TOKEN = internal_api_token(settings.s3_secret_access_key)
_ORCH_INTERNAL_HEADERS = {INTERNAL_TOKEN_HEADER: _ORCH_INTERNAL_TOKEN} if _ORCH_INTERNAL_TOKEN else {}
logger = logging.getLogger(__name__)
celery_app = Celery("subtitle_service", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

_SIZE_TO_REPO: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def _resolve_faster_whisper_model(model_name: str, model_dir: Path, *, proxy: str | None = None) -> str:
    model_name = (model_name or "").strip()
    if not model_name:
        return model_name

    # If user provided a local path, use it directly.
    p = Path(model_name)
    if p.exists():
        return str(p)

    # Prefer our persisted models dir so downloads survive container rebuilds.
    repo_id = _SIZE_TO_REPO.get(model_name, model_name)
    local_name = model_name if model_name in _SIZE_TO_REPO else model_name.replace("/", "--")
    dest = model_dir / local_name
    if dest.exists():
        return str(dest)

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception:
        # Fall back to faster-whisper's own downloader/cache.
        return repo_id

    configure_hf_hub_proxy(proxy)

    model_dir.mkdir(parents=True, exist_ok=True)

    tmp = dest.with_name(dest.name + ".downloading")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        try:
            snapshot_download(repo_id=repo_id, local_dir=str(tmp))
        except TypeError:
            snapshot_download(repo_id=repo_id, local_dir=str(tmp))
        shutil.rmtree(dest, ignore_errors=True)
        tmp.replace(dest)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(
            f"failed to download whisper model '{repo_id}' into '{dest}'. "
            "Open Settings → ASR/Whisper to download it first, or set asr.model to an existing path. "
            f"detail={type(e).__name__}: {e}"
        ) from e

    return str(dest)


def _db() -> Session:
    SessionLocal = get_sessionmaker(settings.database_url)
    return SessionLocal()


def _ensure_db() -> None:
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    auto_migrate(settings.database_url)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# Task Queue (task-level concurrency)
TASK_QUEUE_LOCK_OWNER = "subtitle_service.task_queue"
_TASK_QUEUE_LOCK_TTL = timedelta(seconds=300)
_TASK_QUEUE_HEARTBEAT_INTERVAL_SECONDS = 30.0
_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS = 10


def _task_queue_expires_at(now: datetime) -> datetime:
    return now + _TASK_QUEUE_LOCK_TTL


def _task_queue_is_task_locked(task: Task, now: datetime) -> bool:
    return bool(task.lock_owner == TASK_QUEUE_LOCK_OWNER and task.lock_until and task.lock_until > now)


def _task_queue_unlock(task: Task) -> None:
    task.lock_owner = None
    task.lock_until = None


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


def _clamp_text(text: str, max_len: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1] + "…"


def _build_bilibili_desc(youtube_desc: str, source_url: str) -> str:
    src = (source_url or "").strip()
    tail = f"\n\n原视频：{src}" if src else ""
    max_len = 2000
    if not tail:
        return _clamp_text((youtube_desc or "").strip(), max_len)
    if len(tail) >= max_len:
        return _clamp_text(tail, max_len)
    base = (youtube_desc or "").strip()
    avail = max_len - len(tail)
    if len(base) > avail:
        base = _clamp_text(base, avail)
    out = (base + tail).strip() if base else f"原视频：{src}"
    return _clamp_text(out, max_len)


def _translate_title_openai(
    title: str,
    *,
    target_lang: str,
    style: str,
    translate_settings: dict[str, Any],
) -> str:
    if not title.strip():
        return title
    if not translate_settings.get("openai_api_key"):
        return title
    translated = translate_segments_openai(
        [Segment(start=0.0, end=1.0, text=title)],
        target_lang=target_lang,
        style=style,
        api_key=translate_settings.get("openai_api_key"),
        base_url=translate_settings.get("openai_base_url"),
        model=translate_settings.get("openai_model"),
        temperature=float(translate_settings.get("openai_temperature") or 0.2),
        timeout_seconds=float(translate_settings.get("openai_timeout_seconds") or 180.0),
        batch_size=1,
        enable_summary=False,
    )
    if not translated:
        return title
    return str(translated[0].text or "").strip() or title


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


def _recover_interrupted_subtitle_jobs() -> None:
    """
    Best-effort recovery for jobs that were marked as running but got interrupted
    by an external restart/crash (e.g. host reboot, container restart, SIGKILL).

    Strategy:
      - Mark the old running job as failed (so it's visible in the UI as a crash).
      - DO NOT auto-resume; user must click "continue/resume" manually.
    """
    _ensure_db()
    db = _db()
    marked = 0
    try:
        running = (
            db.query(SubtitleJob)
            .filter(SubtitleJob.status == SubtitleJobStatus.running)
            .order_by(SubtitleJob.updated_at.asc())
            .with_for_update(skip_locked=True)
            .all()
        )
        for old in running:
            try:
                old.status = SubtitleJobStatus.failed
                old_progress = int(old.progress or 0)
                old_msg = (old.error_message or "").strip()

                detail = (
                    f"检测到 Worker 重启/崩溃：该任务在运行中断开（progress={old_progress}）。"
                    "未自动恢复，请在页面点击“继续/从失败处继续”手动恢复。"
                )
                if old_msg:
                    detail = f"{old_msg}\n{detail}"
                old.error_message = detail[:2000]
                db.add(old)
                task = db.get(Task, old.task_id)
                if task and task.status not in {TaskStatus.published, TaskStatus.canceled}:
                    if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
                        _task_queue_unlock(task)
                    task.status = TaskStatus.failed
                    task.error_code = "SUBTITLE_CRASHED"
                    task.error_message = "subtitle job crashed; manual resume required"
                    db.add(task)
                db.commit()
                marked += 1
            except Exception:
                logger.exception("failed to mark interrupted subtitle job as failed (job_id=%s)", getattr(old, "id", None))
                db.rollback()
        if marked:
            logger.warning("marked %s interrupted subtitle job(s) as failed", marked)
    finally:
        db.close()


def _recover_interrupted_render_jobs() -> None:
    _ensure_db()
    db = _db()
    recovered = 0
    try:
        running = (
            db.query(RenderJob)
            .filter(RenderJob.status == RenderJobStatus.running)
            .order_by(RenderJob.updated_at.asc())
            .with_for_update(skip_locked=True)
            .all()
        )
        for j in running:
            try:
                msg = (j.error_message or "").strip()
                detail = "检测到 Worker 重启/崩溃：ffmpeg 压制中断。已自动重新排队。"
                j.error_message = f"{msg}\n{detail}" if msg else detail
                j.retry_count = int(j.retry_count or 0) + 1
                j.status = RenderJobStatus.queued
                j.progress = 0
                j.started_at = None
                j.finished_at = None
                db.add(j)
                db.commit()
                recovered += 1
            except Exception:
                logger.exception("failed to recover render job (render_job_id=%s)", getattr(j, "id", None))
                db.rollback()
        if recovered:
            logger.warning("recovered %s interrupted render job(s)", recovered)
    finally:
        db.close()


@worker_init.connect
def _on_worker_init(**_kwargs: Any) -> None:
    # Only runs in the celery worker process.
    try:
        _recover_interrupted_subtitle_jobs()
        _recover_interrupted_render_jobs()
        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
    except Exception:
        logger.exception("subtitle job recovery failed")


@celery_app.task(name="subtitle_service.process_job")
def process_job(job_id: str) -> dict[str, str]:
    _ensure_db()
    store = S3Store(settings)
    store.ensure_bucket()

    jid = uuid.UUID(job_id)
    db = _db()
    log_path: Path | None = None
    log_key: str | None = None
    hb: _TaskQueueHeartbeat | None = None
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
        if task.lock_owner != TASK_QUEUE_LOCK_OWNER:
            # Not claimed by the task-level scheduler; put it back and wait.
            job.status = SubtitleJobStatus.queued
            db.add(job)
            db.commit()
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
        db.commit()

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()

        req = job.request_json
        input_key = (req.get("input") or {}).get("key")
        if not input_key:
            raise ValueError("missing input.key")

        work_root = Path(settings.work_dir) / "subtitle" / str(job.id)
        work_root.mkdir(parents=True, exist_ok=True)

        video_path = work_root / "input.mp4"
        audio_path = work_root / "audio.wav"
        segments_path = work_root / "segments.json"
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
        video_crf = render_cfg.get("video_crf")
        video_preset = render_cfg.get("video_preset")

        want_ass = "ass" in formats
        need_ass = want_ass or burn_in

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
            f"subtitle job start: job_id={job.id} task_id={task.id} resume={resume} formats={formats} burn_in={burn_in} soft_sub={soft_sub}",
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

        srt_asset = _download_latest_asset(AssetKind.subtitle_srt, srt_path) if resume else None
        if resume and srt_asset:
            srt_key = srt_asset.storage_key
            _safe_append_log_line(log_path, f"resume: found existing subtitle_srt asset: {srt_key}")
            _safe_upload_log(store, log_path, log_key)
            if task.status in {TaskStatus.failed, TaskStatus.created, TaskStatus.ingested, TaskStatus.downloaded, TaskStatus.audio_extracted, TaskStatus.asr_done, TaskStatus.translated}:
                task.status = TaskStatus.subtitle_ready
                db.add(task)
            job.progress = 80
            db.add(job)
            db.commit()

            if need_ass:
                ass_asset = _download_latest_asset(AssetKind.subtitle_ass, ass_path)
                if ass_asset:
                    ass_key = ass_asset.storage_key
                else:
                    segs = srt_to_segments(srt_path.read_text(encoding="utf-8"))
                    ass_text = segments_to_ass(segs, style_name=render_cfg.get("ass_style", "clean_white"))
                    ass_path.write_text(ass_text, encoding="utf-8")
                    ass_sha = sha256_file(ass_path)
                    ass_key = f"sub/{task.id}/subtitle_zh_{ass_sha[:16]}.ass"
                    store.upload_file(ass_path, ass_key, content_type="text/plain")
                    db.add(
                        Asset(
                            task_id=task.id,
                            kind=AssetKind.subtitle_ass,
                            storage_key=ass_key,
                            sha256=ass_sha,
                            size_bytes=ass_path.stat().st_size,
                        )
                    )
                    db.add(Subtitle(task_id=task.id, version=1, format=SubtitleFormat.ass, language="zh", storage_key=ass_key))
                    db.commit()
                    _safe_append_log_line(log_path, f"generated ass from resumed srt: {ass_key}")
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

        video_downloaded = False

        def _ensure_video() -> None:
            nonlocal video_downloaded
            if video_downloaded:
                return
            store.download_file(input_key, video_path)
            video_downloaded = True

        segments: list[Segment] | None = None
        segments_asset = _download_latest_asset(AssetKind.segments_json, segments_path) if resume else None
        if resume and segments_asset:
            segments_key = segments_asset.storage_key
            try:
                data = json.loads(segments_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    tmp: list[Segment] = []
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        start = float(item.get("start") or 0.0)
                        end = float(item.get("end") or 0.0)
                        text = str(item.get("text") or "").strip()
                        if not text:
                            continue
                        tmp.append(Segment(start=start, end=end, text=text, confidence=item.get("confidence")))
                    if tmp:
                        segments = tmp
            except Exception:
                segments = None

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
                audio_key = f"work/{task.id}/audio_{audio_sha[:16]}.wav"
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

            job.progress = 25
            db.add(job)
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
            else:
                raise ValueError(f"unsupported ASR engine: {engine}")
            segments_json = [seg.__dict__ for seg in segments]
            write_json(segments_path, segments_json)
            segments_sha = sha256_file(segments_path)
            segments_key = f"sub/{task.id}/segments_{segments_sha[:16]}.json"
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
            _safe_append_log_line(log_path, f"asr done: segments={len(segments)}")
            _safe_upload_log(store, log_path, log_key)

        job.progress = 60
        db.add(job)
        db.commit()

        translate_cfg = req.get("translate") or {}
        translate_enabled = bool(translate_cfg.get("enabled"))
        target_lang = (translate_cfg.get("target_lang") or "zh").strip() or "zh"
        provider = (translate_cfg.get("provider") or "mock").strip() or "mock"
        bilingual = bool(translate_cfg.get("bilingual"))
        segments_out = segments
        translation_summary = ""
        if translate_enabled:
            _safe_append_log_line(log_path, f"translate: provider={provider} target_lang={target_lang} bilingual={bilingual}")
            translate_settings = get_translate_settings(db, settings)
            style = (translate_cfg.get("style") or translate_settings["default_style"]).strip() or translate_settings["default_style"]
            batch_size = int(translate_cfg.get("batch_size") or translate_settings["default_batch_size"])
            enable_summary_val = translate_cfg.get("enable_summary")
            enable_summary = translate_settings["default_enable_summary"] if enable_summary_val is None else bool(enable_summary_val)

            max_retries = int(translate_settings.get("default_max_retries") or 0)

            def _is_retryable_translate_error(err: Exception) -> bool:
                msg = str(err or "")
                if "api key is not set" in msg.lower():
                    return False
                return True

            def _sleep_retry(attempt: int) -> None:
                # Exponential backoff: 2s, 4s, 8s... capped at 30s.
                delay = min(30.0, float(2 ** max(0, attempt)))
                time.sleep(delay)

            attempt = 0
            while True:
                try:
                    if provider == "mock":
                        segments_out = translate_segments_mock(segments, target_lang=target_lang)
                    elif provider in {"noop", "none"}:
                        segments_out = segments
                    elif provider == "openai":
                        segments_out, translation_summary = translate_segments_openai_with_summary(
                            segments,
                            target_lang=target_lang,
                            style=style,
                            api_key=translate_settings["openai_api_key"],
                            base_url=translate_settings["openai_base_url"],
                            model=translate_settings["openai_model"],
                            temperature=translate_settings["openai_temperature"],
                            timeout_seconds=translate_settings["openai_timeout_seconds"],
                            batch_size=batch_size,
                            enable_summary=enable_summary,
                        )
                    else:
                        raise ValueError(f"unsupported translate provider: {provider}")
                    job.error_message = None
                    db.add(job)
                    db.commit()
                    break
                except Exception as e:
                    if provider != "openai" or attempt >= max_retries or not _is_retryable_translate_error(e):
                        raise
                    attempt += 1
                    job.error_message = f"translate failed; retrying ({attempt}/{max_retries}): {e}"
                    db.add(job)
                    db.commit()
                    _safe_append_log_line(log_path, f"translate retry {attempt}/{max_retries}: {type(e).__name__}: {e}")
                    _safe_upload_log(store, log_path, log_key)
                    _sleep_retry(attempt)
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
                                translate_settings=translate_settings,
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
                        tags = generate_bilibili_tags_openai(
                            title=title_hint,
                            summary=translation_summary,
                            transcript=transcript_excerpt,
                            api_key=translate_settings.get("openai_api_key"),
                            base_url=translate_settings.get("openai_base_url"),
                            model=translate_settings.get("openai_model"),
                            temperature=float(translate_settings.get("openai_temperature") or 0.2),
                            timeout_seconds=float(translate_settings.get("openai_timeout_seconds") or 180.0),
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
                            text=f"{zh_seg.text}\n{src_seg.text}",
                            confidence=zh_seg.confidence,
                        )
                    )
                segments_out = merged

        srt_text = segments_to_srt(segments_out)
        srt_path.write_text(srt_text, encoding="utf-8")
        srt_sha = sha256_file(srt_path)
        srt_key = f"sub/{task.id}/subtitle_zh_{srt_sha[:16]}.srt"
        store.upload_file(srt_path, srt_key, content_type="text/plain")
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
            ass_text = segments_to_ass(segments_out, style_name=render_cfg.get("ass_style", "clean_white"))
            ass_path.write_text(ass_text, encoding="utf-8")
            ass_sha = sha256_file(ass_path)
            ass_key = f"sub/{task.id}/subtitle_zh_{ass_sha[:16]}.ass"
            store.upload_file(ass_path, ass_key, content_type="text/plain")
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.subtitle_ass,
                    storage_key=ass_key,
                    sha256=ass_sha,
                    size_bytes=ass_path.stat().st_size,
                )
            )
            db.add(Subtitle(task_id=task.id, version=1, format=SubtitleFormat.ass, language="zh", storage_key=ass_key))
            _safe_append_log_line(log_path, f"subtitle ass uploaded: {ass_key}")

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
        if hb is not None:
            hb.stop()
        db.close()


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
    unlocked_expired = 0

    to_start: list[tuple[str, str]] = []  # ("subtitle"|"render", job_id)
    try:
        _task_queue_lock_settings_row(db)
        cfg = get_task_queue_settings(db)
        try:
            max_conc = int(cfg.get("max_concurrency", 1))
        except Exception:
            max_conc = 1
        if max_conc < 0:
            max_conc = 0
        if max_conc == 0:
            return {"status": "paused", "max_concurrency": str(max_conc)}

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

        # Recover render jobs that were marked as running by the scheduler but never claimed by a worker.
        cutoff = now - timedelta(seconds=60)
        # Subtitle jobs: scheduler sets progress=1; worker bumps it to >=2 once claimed.
        stuck_subtitle = (
            db.query(SubtitleJob)
            .filter(
                SubtitleJob.status == SubtitleJobStatus.running,
                SubtitleJob.progress <= 1,
                # If the worker actually started, it sets logs_key very early. Treat jobs without logs_key
                # as "scheduler started but worker never claimed".
                SubtitleJob.logs_key.is_(None),
                SubtitleJob.updated_at.is_not(None),
                SubtitleJob.updated_at < cutoff,
            )
            .order_by(SubtitleJob.updated_at.asc(), SubtitleJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .all()
        )
        for j in stuck_subtitle:
            msg = (j.error_message or "").strip()
            detail = "检测到疑似 Worker 未消费：subtitle job 长时间停留在 running(1%)。已自动重新排队。"
            j.error_message = f"{msg}\n{detail}" if msg else detail
            j.status = SubtitleJobStatus.queued
            j.progress = 0
            db.add(j)
            recovered_subtitle += 1

        stuck = (
            db.query(RenderJob)
            .filter(
                RenderJob.status == RenderJobStatus.running,
                RenderJob.progress <= 1,
                RenderJob.started_at.is_not(None),
                RenderJob.started_at < cutoff,
            )
            .order_by(RenderJob.started_at.asc(), RenderJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .all()
        )
        for j in stuck:
            msg = (j.error_message or "").strip()
            detail = "检测到疑似 Worker 未消费：render job 长时间停留在 running(1%)。已自动重新排队。"
            j.error_message = f"{msg}\n{detail}" if msg else detail
            j.retry_count = int(j.retry_count or 0) + 1
            j.status = RenderJobStatus.queued
            j.progress = 0
            j.started_at = None
            j.finished_at = None
            db.add(j)
            recovered_render += 1

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
                rj.status = RenderJobStatus.running
                rj.progress = max(int(rj.progress or 0), 1)
                rj.started_at = now
                db.add(rj)
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
                sj.status = SubtitleJobStatus.running
                sj.progress = max(int(sj.progress or 0), 1)
                db.add(sj)
                to_start.append(("subtitle", str(sj.id)))
                started_subtitle += 1

        # Phase 2: lock and start new tasks up to max_concurrency.
        running_tasks = len(locked_tasks)
        capacity = max(0, max_conc - running_tasks)
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
                rj.status = RenderJobStatus.running
                rj.progress = max(int(rj.progress or 0), 1)
                rj.started_at = now
                db.add(task)
                db.add(rj)
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
            sj.status = SubtitleJobStatus.running
            sj.progress = max(int(sj.progress or 0), 1)
            db.add(task)
            db.add(sj)
            to_start.append(("subtitle", str(sj.id)))
            started_subtitle += 1
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

    return {
        "status": "ok",
        "max_concurrency": str(max_conc),
        "started_subtitle": str(started_subtitle),
        "started_render": str(started_render),
        "recovered_subtitle": str(recovered_subtitle),
        "recovered_render": str(recovered_render),
        "unlocked_expired": str(unlocked_expired),
    }


@celery_app.task(name="subtitle_service.render_queue_tick")
def render_queue_tick() -> dict[str, Any]:
    # Legacy alias.
    return task_queue_tick()


@celery_app.task(name="subtitle_service.process_render_job")
def process_render_job(render_job_id: str) -> dict[str, Any]:
    _ensure_db()
    store = S3Store(settings)
    store.ensure_bucket()

    rid = uuid.UUID(render_job_id)
    db = _db()
    log_path: Path | None = None
    log_key: str | None = None
    hb: _TaskQueueHeartbeat | None = None
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
        if task.lock_owner != TASK_QUEUE_LOCK_OWNER:
            # Not claimed by the task-level scheduler; put it back and wait.
            if rj.status != RenderJobStatus.succeeded:
                rj.status = RenderJobStatus.queued
                rj.started_at = None
                rj.progress = 0
                db.add(rj)
                db.commit()
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

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()

        # Best-effort: if called directly, try to claim it.
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
        db.commit()

        req = rj.request_json if isinstance(rj.request_json, dict) else {}
        input_key = str(req.get("input_key") or "").strip()
        srt_key = str(req.get("srt_key") or "").strip()
        ass_key = str(req.get("ass_key") or "").strip() or None
        burn_in = bool(req.get("burn_in"))
        soft_sub = bool(req.get("soft_sub"))
        render_cfg = req.get("render") if isinstance(req.get("render"), dict) else {}

        video_codec = str(render_cfg.get("video_codec") or "av1").strip().lower() or "av1"
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
            f"render job start: render_job_id={rj.id} task_id={task.id} burn_in={burn_in} soft_sub={soft_sub} codec={video_codec} preset={video_preset} crf={video_crf}",
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
                preset=video_preset,
                crf=video_crf,
                log_path=log_path,
                live_upload_cb=_live_upload_log,
            )
            _safe_upload_log(store, log_path, log_key)
            final_sha = sha256_file(out_video)
            final_key = f"final/{task.id}/video_burnin_{final_sha[:16]}.mp4"
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
            final_key = f"final/{task.id}/video_softsub_{final_sha[:16]}.mkv"
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
        db.commit()
        _safe_append_log_line(log_path, "render job done")
        _safe_upload_log(store, log_path, log_key)

        if task.lock_owner == TASK_QUEUE_LOCK_OWNER:
            _task_queue_unlock(task)
            db.add(task)
            db.commit()

        # Trigger optional after_render actions (e.g. auto publish).
        after_render = req.get("after_render") if isinstance(req, dict) else None
        if isinstance(after_render, dict) and after_render.get("publish"):
            celery_app.send_task("subtitle_service.after_render_publish", args=[str(rj.id)], queue="subtitle")

        celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
        return {"status": "ok"}
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
        if hb is not None:
            hb.stop()
        db.close()


@celery_app.task(name="subtitle_service.after_render_publish")
def after_render_publish(render_job_id: str) -> dict[str, Any]:
    _ensure_db()
    db = _db()
    orch_base = str(settings.orchestrator_url or "").strip().rstrip("/") or "http://localhost:8000"
    try:
        rid = uuid.UUID(render_job_id)
        rj = db.get(RenderJob, rid)
        if not rj:
            return {"status": "error", "detail": "render job not found"}

        task = db.get(Task, rj.task_id)
        if not task:
            return {"status": "error", "detail": "task not found"}
        if task.status == TaskStatus.published:
            return {"status": "skipped", "detail": "already published"}

        req = rj.request_json if isinstance(rj.request_json, dict) else {}
        after_render = req.get("after_render") if isinstance(req.get("after_render"), dict) else {}
        if not after_render.get("publish"):
            return {"status": "skipped"}

        publish_payload = after_render.get("publish_payload") or after_render.get("payload")
        if not isinstance(publish_payload, dict):
            return {"status": "error", "detail": "after_render.publish_payload is missing"}

        # Ensure we don't accidentally override the latest rendered asset selection.
        if publish_payload.get("video_key") in {"", None}:
            publish_payload["video_key"] = None

        with httpx.Client(timeout=30.0, headers=_ORCH_INTERNAL_HEADERS) as client:
            resp = client.post(f"{orch_base}/tasks/{task.id}/actions/publish", json=publish_payload)
            resp.raise_for_status()

        return {"status": "ok"}
    except Exception as e:
        task = db.get(Task, rj.task_id) if "rj" in locals() and rj else None
        if task:
            task.status = TaskStatus.failed
            task.error_message = str(e)
            db.add(task)
            db.commit()
        return {"status": "error", "detail": str(e)}
    finally:
        db.close()


@celery_app.task(name="subtitle_service.cleanup_task")
def cleanup_task(task_id: str) -> dict[str, Any]:
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
        task = db.get(Task, tid)
        if not task:
            return {"status": "error", "detail": "task not found"}
        if task.status != TaskStatus.published:
            return {"status": "skipped", "detail": f"task status is {task.status.value}"}

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
            return {"status": "skipped", "detail": f"in-flight jobs: subtitle={in_flight_sub} render={in_flight_render}"}

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
    except Exception as e:
        logger.exception("cleanup task failed (task_id=%s)", task_id)
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


@celery_app.task(name="subtitle_service.auto_youtube_pipeline")
def auto_youtube_pipeline(task_id: str) -> dict[str, str]:
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
        task = db.get(Task, tid)
        if not task:
            raise RuntimeError("task not found")
        if task.source_type.value != "youtube":
            raise RuntimeError("task is not a youtube source")

        now = _now()
        if not _task_queue_is_task_locked(task, now):
            # Claim a task slot before doing anything heavy (download/ASR/render).
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
                    args=[str(tid)],
                    queue="subtitle",
                    countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS,
                )
                return {"status": "queued", "task_id": str(tid), "detail": "task queue paused"}

            running = (
                db.query(Task)
                .filter(Task.lock_owner == TASK_QUEUE_LOCK_OWNER, Task.lock_until.is_not(None), Task.lock_until > now)
                .count()
            )
            if int(running or 0) >= int(max_conc):
                celery_app.send_task(
                    "subtitle_service.auto_youtube_pipeline",
                    args=[str(tid)],
                    queue="subtitle",
                    countdown=_TASK_QUEUE_REQUEUE_COUNTDOWN_SECONDS,
                )
                return {"status": "queued", "task_id": str(tid), "detail": "waiting for task queue"}

            task.lock_owner = TASK_QUEUE_LOCK_OWNER
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)
            db.commit()
            acquired_lock = True
        elif task.lock_until is None or task.lock_until <= now:
            # Refresh a stale/expired lock to avoid accidental eviction mid-pipeline.
            task.lock_until = _task_queue_expires_at(now)
            db.add(task)
            db.commit()

        hb = _TaskQueueHeartbeat(task.id)
        hb.start()

        profile = get_auto_profile(db)

        # Download YouTube video + cover + metadata (idempotent).
        yt: dict[str, Any] = {}
        yt_attempt = 0
        yt_max_retries = 2
        while True:
            try:
                timeout_seconds = float(getattr(settings, "orchestrator_timeout_seconds", 1800.0) or 1800.0)
                with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=10.0), headers=_ORCH_INTERNAL_HEADERS) as client:
                    resp = client.post(f"{orch_base}/tasks/{tid}/actions/youtube_download")
                    resp.raise_for_status()
                    yt = resp.json() if resp.content else {}
                break
            except httpx.HTTPStatusError as e:
                status_code = int(getattr(e.response, "status_code", 0) or 0)
                if status_code in {429, 500, 502, 503, 504} and yt_attempt < yt_max_retries:
                    yt_attempt += 1
                    try:
                        task.retry_count = int(task.retry_count or 0) + 1
                        msg = (e.response.text or "").strip()
                        if len(msg) > 300:
                            msg = msg[:299] + "…"
                        task.error_message = f"youtube_download failed; retrying ({yt_attempt}/{yt_max_retries}): {status_code} {msg}".strip()
                        db.add(task)
                        db.commit()
                    except Exception:
                        db.rollback()
                    time.sleep(min(30.0, float(3 * (2**yt_attempt))))
                    continue
                raise
            except httpx.HTTPError as e:
                if yt_attempt < yt_max_retries:
                    yt_attempt += 1
                    try:
                        task.retry_count = int(task.retry_count or 0) + 1
                        task.error_message = f"youtube_download request failed; retrying ({yt_attempt}/{yt_max_retries}): {type(e).__name__}: {e}"
                        db.add(task)
                        db.commit()
                    except Exception:
                        db.rollback()
                    time.sleep(min(30.0, float(3 * (2**yt_attempt))))
                    continue
                raise

        if yt_attempt:
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
                        "video_preset": profile.get("video_preset"),
                        "video_crf": profile.get("video_crf"),
                    },
                },
                "output_prefix": f"sub/{tid}/",
            }

            job = SubtitleJob(task_id=tid, request_json=req, status=SubtitleJobStatus.queued, progress=0)
            db.add(job)
            db.commit()
            db.refresh(job)

            # If auto_publish is enabled, defer publishing until render finishes.
            if profile.get("auto_publish"):
                meta_model = get_bilibili_publish_settings(db)["default_meta"]
                meta = meta_model.model_dump() if hasattr(meta_model, "model_dump") else dict(meta_model)

                translate_settings = get_translate_settings(db, settings)
                title_out = yt_title or str(meta.get("title") or "").strip() or "未命名"
                if profile.get("publish_translate_title") and title_out and not _has_cjk(title_out):
                    provider = str(profile.get("translate_provider") or translate_settings.get("default_provider") or "").strip() or "openai"
                    if provider == "openai":
                        try:
                            title_out = _translate_title_openai(
                                title_out,
                                target_lang=str(profile.get("target_lang") or translate_settings.get("default_target_lang") or "zh"),
                                style=str(profile.get("translate_style") or translate_settings.get("default_style") or "口语自然"),
                                translate_settings=translate_settings,
                            )
                        except Exception:
                            pass

                prefix = str(profile.get("publish_title_prefix") or "").strip()
                if prefix and not title_out.startswith(prefix):
                    title_out = prefix + title_out
                meta["title"] = _clamp_text(title_out, 80) or _clamp_text(yt_title, 80) or "未命名"
                meta["desc"] = _build_bilibili_desc(yt_desc, webpage_url)
                if bool(profile.get("publish_enable_reprint", True)):
                    meta["copyright"] = 2
                    meta["source"] = webpage_url
                else:
                    meta["copyright"] = 1
                    meta["source"] = ""

                # Persist publish meta as a file so it can be edited before publishing.
                publish_meta_key = f"meta/{tid}/publish_meta.json"
                store.put_bytes(
                    json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
                    publish_meta_key,
                    content_type="application/json",
                )

                # Let orchestrator pick the latest rendered asset when publishing.
                publish_payload = {
                    "account_id": None,
                    "video_key": None,
                    "cover_key": cover_key,
                    "typeid_mode": profile.get("publish_typeid_mode") or "ai_summary",
                    "meta": None,
                }

                req["after_render"] = {"publish": True, "publish_payload": publish_payload}
                job.request_json = req
                db.add(job)
                db.commit()

            celery_app.send_task("subtitle_service.task_queue_tick", args=[], queue="subtitle")
            return {"status": "ok", "task_id": str(tid), "detail": f"queued subtitle job {job.id}"}

        if profile.get("auto_publish"):
            task = db.get(Task, tid)
            if task and task.status == TaskStatus.published:
                return {"status": "ok", "task_id": str(tid), "detail": "already published"}

            if not final_asset:
                raise RuntimeError("no final video asset found; enable burn_in/soft_sub in auto profile")

            meta_model = get_bilibili_publish_settings(db)["default_meta"]
            meta = meta_model.model_dump() if hasattr(meta_model, "model_dump") else dict(meta_model)

            translate_settings = get_translate_settings(db, settings)
            title_out = yt_title or str(meta.get("title") or "").strip() or "未命名"
            if profile.get("publish_translate_title") and title_out and not _has_cjk(title_out):
                provider = str(profile.get("translate_provider") or translate_settings.get("default_provider") or "").strip() or "openai"
                if provider == "openai":
                    try:
                        title_out = _translate_title_openai(
                            title_out,
                            target_lang=str(profile.get("target_lang") or translate_settings.get("default_target_lang") or "zh"),
                            style=str(profile.get("translate_style") or translate_settings.get("default_style") or "口语自然"),
                            translate_settings=translate_settings,
                        )
                    except Exception:
                        pass

            prefix = str(profile.get("publish_title_prefix") or "").strip()
            if prefix and not title_out.startswith(prefix):
                title_out = prefix + title_out
            meta["title"] = _clamp_text(title_out, 80) or _clamp_text(yt_title, 80) or "未命名"
            meta["desc"] = _build_bilibili_desc(yt_desc, webpage_url)
            if bool(profile.get("publish_enable_reprint", True)):
                meta["copyright"] = 2
                meta["source"] = webpage_url
            else:
                meta["copyright"] = 1
                meta["source"] = ""

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

            with httpx.Client(timeout=30.0, headers=_ORCH_INTERNAL_HEADERS) as client:
                resp = client.post(f"{orch_base}/tasks/{tid}/actions/publish", json=publish_payload)
                resp.raise_for_status()

        return {"status": "ok", "task_id": str(tid)}
    except Exception as e:
        task = db.get(Task, uuid.UUID(task_id))
        if task:
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
