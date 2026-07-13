from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celery import Celery
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from videoroll.ai.service import AIService
from videoroll.apps.bilibili_publisher.auth_settings_store import get_bilibili_cookie_header, get_bilibili_csrf_token
from celery.exceptions import Retry

from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliDescTooLongError, BilibiliRateLimitError, BilibiliWebClient
from videoroll.apps.bilibili_publisher.constants import BILIBILI_DESC_RETRY_MAX_CHARS
from videoroll.apps.orchestrator_api.youtube_downloader import extract_youtube_channel_info, pick_thumbnail_url
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta
from videoroll.apps.bilibili_publisher.storage_keys import unique_publish_result_key
from videoroll.apps.bilibili_publisher.typeid_recommender import flatten_typelist
from videoroll.apps.publish_meta_rules import bilibili_text_units, clamp_bilibili_text
from videoroll.apps.publish_lifecycle import enqueue_publish_batch_cleanup, reconcile_publish_batch
from videoroll.apps.outbox.worker_inbox import (
    OperationHeartbeat,
    claim_outbox_operation,
    finish_operation,
    release_operation,
)
from videoroll.config import get_bilibili_publisher_settings, get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.auto_migrate import auto_migrate
from videoroll.db.models import Asset, AssetKind, Platform, PublishJob, PublishState, Task, TaskStatus
from videoroll.db.session import get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_summary
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings


settings = get_bilibili_publisher_settings()
logger = logging.getLogger(__name__)
_DB_READY_LOCK = threading.Lock()
_DB_READY_PID: int | None = None
_REDIS_LOCK = threading.Lock()
_REDIS_PID: int | None = None
_REDIS_CLIENT: Redis | None = None
celery_app = Celery("bilibili_publisher", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)


def _db() -> Session:
    SessionLocal = get_sessionmaker(settings.database_url)
    return SessionLocal()


def _fresh_translate_settings() -> dict[str, Any]:
    SessionLocal = get_sessionmaker(settings.database_url)
    db = SessionLocal()
    try:
        return get_translate_settings(db, get_subtitle_settings())
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


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return value if value >= minimum else minimum


def _extract_video_key(meta_json: dict[str, Any]) -> str:
    v = meta_json.get("video")
    if isinstance(v, dict):
        key = v.get("key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    key = meta_json.get("video_key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return ""


def _read_s3_json(store: S3Store, key: str) -> dict[str, Any]:
    obj = store.get_object(key)
    body = obj.get("Body")
    if not body:
        return {}
    try:
        raw = body.read() or b""
    finally:
        try:
            body.close()
        except Exception:
            pass
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_latest_youtube_info(task: Task, db: Session, store: S3Store) -> dict[str, Any]:
    if task.source_type.value != "youtube":
        return {}

    asset = (
        db.query(Asset)
        .filter(Asset.task_id == task.id, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not asset:
        return {}
    try:
        return _read_s3_json(store, asset.storage_key)
    except Exception:
        return {}


def _normalize_collection_title(value: Any, *, max_chars: int = 80) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _youtube_collection_title_from_info(info: dict[str, Any]) -> str:
    if not isinstance(info, dict):
        return ""
    for key in ("uploader", "channel", "uploader_id", "channel_id"):
        title = _normalize_collection_title(info.get(key))
        if title:
            return title
    return ""


def _youtube_channel_url_from_info(info: dict[str, Any]) -> str:
    if not isinstance(info, dict):
        return ""
    for key in ("uploader_url", "channel_url", "webpage_url"):
        value = str(info.get(key) or "").strip()
        if value and "youtube.com/" in value:
            return value
    return ""


def _pick_youtube_collection_cover(
    *,
    task: Task,
    store: S3Store,
    db: Session,
    fallback_cover_url: str,
) -> tuple[str, dict[str, Any]]:
    info = _read_latest_youtube_info(task, db, store)
    channel_url = _youtube_channel_url_from_info(info)
    if channel_url:
        try:
            channel_info = extract_youtube_channel_info(channel_url, get_subtitle_settings())
            art_url = str(pick_thumbnail_url(channel_info) or "").strip()
            if art_url:
                return art_url, {"source": "youtube_channel", "channel_url": channel_url}
        except Exception as e:
            logger.warning("failed to extract youtube channel art (task_id=%s url=%s): %s", task.id, channel_url, e)

    fallback = str(fallback_cover_url or "").strip()
    if fallback:
        return fallback, {"source": "video_cover"}
    return "", {"source": "none"}


def _attach_youtube_uploader_collection(
    *,
    client: BilibiliWebClient,
    task: Task,
    meta: BilibiliPublishMeta,
    uploaded_cid: int,
    aid: int,
    csrf: str,
    cover_url: str,
    store: S3Store,
    db: Session,
) -> dict[str, Any]:
    info = _read_latest_youtube_info(task, db, store)
    title = _youtube_collection_title_from_info(info)
    if not title:
        return {"ok": False, "skipped": True, "reason": "youtube uploader/channel not found"}

    result: dict[str, Any] = {
        "ok": False,
        "title": title,
        "created": False,
    }
    try:
        season = client.find_season_by_title(title)
        cover_debug: dict[str, Any] = {"source": "existing_season"} if season else {}
        if season is None:
            season_cover, cover_debug = _pick_youtube_collection_cover(
                task=task,
                store=store,
                db=db,
                fallback_cover_url=cover_url,
            )
            if not season_cover:
                video_info = client.get_video_info(aid=aid)
                season_cover = video_info.cover
                cover_debug = {"source": "published_video"}
            if not season_cover:
                return {
                    **result,
                    "skipped": True,
                    "reason": "collection cover is empty",
                    "cover": cover_debug,
                }
            season, created = client.ensure_season(title=title, cover=season_cover, csrf=csrf)
            result["created"] = bool(created)

        episodes = [
            {
                "aid": int(aid),
                "cid": int(uploaded_cid),
                "title": meta.title,
                "charging_pay": 0,
            }
        ]
        try:
            client.add_to_season(section_id=season.section_id, episodes=episodes, csrf=csrf)
        except Exception:
            video_info = client.get_video_info(aid=aid)
            client.add_to_season(
                section_id=season.section_id,
                episodes=[
                    {
                        "aid": int(video_info.aid),
                        "cid": int(video_info.cid),
                        "title": video_info.title,
                        "charging_pay": 0,
                    }
                ],
                csrf=csrf,
            )

        return {
            **result,
            "ok": True,
            "season_id": season.season_id,
            "section_id": season.section_id,
            "cover": cover_debug,
        }
    except Exception as e:
        return {
            **result,
            "error": str(e),
        }


def _ensure_publish_result_asset(db: Session, task_id: uuid.UUID, key: str) -> None:
    existing = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.publish_result, Asset.storage_key == key)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if existing:
        return
    db.add(Asset(task_id=task_id, kind=AssetKind.publish_result, storage_key=key))


def _redis_client() -> Redis | None:
    global _REDIS_PID, _REDIS_CLIENT
    pid = os.getpid()
    if _REDIS_CLIENT is not None and _REDIS_PID == pid:
        return _REDIS_CLIENT
    with _REDIS_LOCK:
        if _REDIS_CLIENT is not None and _REDIS_PID == pid:
            return _REDIS_CLIENT
        try:
            client = Redis.from_url(settings.redis_url, decode_responses=True)
            client.ping()
        except Exception as e:
            logger.warning("publish throttle disabled: redis unavailable (%s)", e)
            _REDIS_CLIENT = None
            _REDIS_PID = pid
            return None
        _REDIS_CLIENT = client
        _REDIS_PID = pid
        return _REDIS_CLIENT


def _count_pending_publish_jobs(db: Session) -> int:
    return int(
        db.query(Task)
        .filter(Task.status == TaskStatus.publishing)
        .count()
    )


def _publish_throttle_config(stage: str) -> tuple[float, float, float]:
    if stage == "upload":
        return (
            _env_float("BILIBILI_PUBLISH_UPLOAD_BASE_SECONDS", 45.0, minimum=1.0),
            _env_float("BILIBILI_PUBLISH_UPLOAD_QUEUE_STEP_SECONDS", 15.0, minimum=0.0),
            _env_float("BILIBILI_PUBLISH_UPLOAD_MAX_SECONDS", 180.0, minimum=1.0),
        )
    return (
        _env_float("BILIBILI_PUBLISH_SUBMIT_BASE_SECONDS", 20.0, minimum=1.0),
        _env_float("BILIBILI_PUBLISH_SUBMIT_QUEUE_STEP_SECONDS", 8.0, minimum=0.0),
        _env_float("BILIBILI_PUBLISH_SUBMIT_MAX_SECONDS", 90.0, minimum=1.0),
    )


def _compute_publish_throttle_interval(stage: str, *, pending_jobs: int) -> float:
    base, step, cap = _publish_throttle_config(stage)
    queue_size = max(1, int(pending_jobs))
    return min(cap, base + max(0, queue_size - 1) * step)


def _publish_stage_gate_key(stage: str) -> str:
    return f"videoroll:bilibili:publish:{stage}:not_before"


def _publish_stage_lock_key(stage: str) -> str:
    return f"videoroll:bilibili:publish:{stage}:lock"


def _publish_job_lock_key(job_id: str) -> str:
    return f"videoroll:bilibili:publish:job:{job_id}:lock"


def _try_acquire_publish_job_lock(job_id: str) -> Any:
    client = _redis_client()
    if client is None:
        return None
    lock = client.lock(
        _publish_job_lock_key(job_id),
        timeout=int(_env_float("BILIBILI_PUBLISH_JOB_LOCK_TIMEOUT_SECONDS", 3600.0, minimum=60.0)),
        blocking_timeout=0,
    )
    try:
        if not bool(lock.acquire(blocking=False)):
            return False
    except RedisError as e:
        logger.warning("publish job lock disabled for this attempt (job_id=%s): %s", job_id, e)
        return None
    return lock


def _parse_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _reserve_publish_stage_slot(db: Session, *, stage: str, job_id: str) -> dict[str, Any]:
    pending_jobs = _count_pending_publish_jobs(db)
    interval_seconds = _compute_publish_throttle_interval(stage, pending_jobs=pending_jobs)
    out = {
        "stage": stage,
        "job_id": job_id,
        "pending_jobs": pending_jobs,
        "interval_seconds": round(interval_seconds, 3),
        "wait_seconds": 0.0,
        "source": "disabled",
    }

    client = _redis_client()
    if client is None:
        return out

    lock = client.lock(
        _publish_stage_lock_key(stage),
        timeout=int(_env_float("BILIBILI_PUBLISH_THROTTLE_LOCK_TIMEOUT_SECONDS", 15.0, minimum=3.0)),
        blocking_timeout=_env_float("BILIBILI_PUBLISH_THROTTLE_LOCK_WAIT_SECONDS", 5.0, minimum=0.0),
    )
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=True))
        if not acquired:
            out["source"] = "lock_timeout"
            return out
        now = time.time()
        not_before = _parse_float(client.get(_publish_stage_gate_key(stage))) or 0.0
        slot_at = max(now, not_before)
        next_not_before = slot_at + interval_seconds
        ttl_seconds = max(600, int(max(0.0, next_not_before - now) + 600.0))
        client.set(_publish_stage_gate_key(stage), f"{next_not_before:.3f}", ex=ttl_seconds)
        out.update(
            {
                "wait_seconds": round(max(0.0, slot_at - now), 3),
                "slot_at": round(slot_at, 3),
                "next_not_before": round(next_not_before, 3),
                "source": "redis",
            }
        )
        return out
    except RedisError as e:
        logger.warning("publish throttle fallback (stage=%s job_id=%s): %s", stage, job_id, e)
        out["source"] = "redis_error"
        return out
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                pass


def _apply_publish_stage_throttle(db: Session, *, stage: str, job_id: str) -> dict[str, Any]:
    info = _reserve_publish_stage_slot(db, stage=stage, job_id=job_id)
    wait_seconds = float(info.get("wait_seconds") or 0.0)
    if wait_seconds > 0.25:
        logger.info(
            "publish throttle wait (stage=%s job_id=%s pending=%s interval=%.3fs wait=%.3fs source=%s)",
            stage,
            job_id,
            info.get("pending_jobs"),
            float(info.get("interval_seconds") or 0.0),
            wait_seconds,
            info.get("source"),
        )
        time.sleep(wait_seconds)
    return info


def _extend_publish_stage_cooldown(stage: str, *, delay_seconds: float) -> dict[str, Any]:
    client = _redis_client()
    out = {
        "stage": stage,
        "delay_seconds": round(max(0.0, float(delay_seconds)), 3),
        "source": "disabled",
    }
    if client is None:
        return out

    lock = client.lock(
        _publish_stage_lock_key(stage),
        timeout=int(_env_float("BILIBILI_PUBLISH_THROTTLE_LOCK_TIMEOUT_SECONDS", 15.0, minimum=3.0)),
        blocking_timeout=_env_float("BILIBILI_PUBLISH_THROTTLE_LOCK_WAIT_SECONDS", 5.0, minimum=0.0),
    )
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=True))
        if not acquired:
            out["source"] = "lock_timeout"
            return out
        now = time.time()
        not_before = _parse_float(client.get(_publish_stage_gate_key(stage))) or 0.0
        next_not_before = max(not_before, now + max(0.0, float(delay_seconds)))
        ttl_seconds = max(600, int(max(0.0, next_not_before - now) + 600.0))
        client.set(_publish_stage_gate_key(stage), f"{next_not_before:.3f}", ex=ttl_seconds)
        out.update(
            {
                "source": "redis",
                "next_not_before": round(next_not_before, 3),
            }
        )
        return out
    except RedisError as e:
        logger.warning("publish throttle cooldown fallback (stage=%s): %s", stage, e)
        out["source"] = "redis_error"
        return out
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                pass


def _rate_limit_stage(error: BilibiliRateLimitError) -> str:
    scope = str(getattr(error, "scope", "") or "").strip().lower()
    if scope in {"upload", "submit"}:
        return scope
    message = str(getattr(error, "message", "") or "")
    if int(getattr(error, "code", 0) or 0) == 601 or "上传" in message:
        return "upload"
    return "submit"


def _rate_limit_retry_countdown(*, stage: str, retries_done: int) -> float:
    exp = (2 ** (retries_done + 1)) + random.random()
    if stage == "upload":
        return min(180.0, max(60.0, exp))
    return min(90.0, max(20.0, exp))


def _publish_meta_with_retry_desc_limit(meta: BilibiliPublishMeta) -> BilibiliPublishMeta:
    payload = meta.model_dump()
    payload["desc"] = clamp_bilibili_text(meta.desc, BILIBILI_DESC_RETRY_MAX_CHARS)
    # desc_v2 is another rich-text representation of the description; clearing it
    # prevents Bilibili from counting stale raw_text after desc has been shortened.
    payload["desc_v2"] = None
    return BilibiliPublishMeta.model_validate(payload)


def _latest_published_job(
    db: Session,
    task_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
) -> PublishJob | None:
    query = db.query(PublishJob).filter(
        PublishJob.task_id == task_id,
        PublishJob.platform == Platform.bilibili,
        PublishJob.state == PublishState.published,
    )
    if account_id is None:
        query = query.filter(PublishJob.account_id.is_(None))
    else:
        query = query.filter(
            or_(
                PublishJob.account_id == account_id,
                and_(PublishJob.account_id.is_(None), PublishJob.bili_account_id == account_id),
            )
        )
    return query.order_by(PublishJob.updated_at.desc(), PublishJob.created_at.desc()).first()


def _task_has_published_job(db: Session, task_id: uuid.UUID) -> bool:
    return _latest_published_job(db, task_id) is not None


def _can_mark_task_publish_failed(db: Session, task: Task) -> bool:
    return task.status != TaskStatus.published and not _task_has_published_job(db, task.id)


def _reconcile_batch_after_publish_job(db: Session, job: PublishJob) -> bool:
    if job.batch_id is None:
        return False
    return reconcile_publish_batch(db, job.batch_id).cleanup_needed


def _mirror_published_job(job: PublishJob, published_job: PublishJob | None, task: Task) -> None:
    job.state = PublishState.published
    job.aid = published_job.aid if published_job else job.aid
    job.bvid = published_job.bvid if published_job else job.bvid
    job.external_id = published_job.external_id if published_job else job.external_id
    job.external_url = published_job.external_url if published_job else job.external_url
    job.response_json = {
        **_as_dict(published_job.response_json if published_job else job.response_json),
        "skipped_duplicate_publish": True,
        "reason": "task already published",
    }
    job.updated_at = _utcnow()
    if job.batch_id is None:
        task.status = TaskStatus.published
        task.error_code = None
        task.error_message = None


def _add_archive_with_desc_retry(
    *,
    client: BilibiliWebClient,
    meta: BilibiliPublishMeta,
    csrf: str,
    tid: int,
    uploaded: Any,
    cover_url: str,
) -> tuple[dict[str, Any], BilibiliPublishMeta, dict[str, Any] | None]:
    try:
        return client.add_archive(meta, csrf=csrf, tid=tid, uploaded=uploaded, cover_url=cover_url), meta, None
    except BilibiliDescTooLongError as e:
        retry_meta = _publish_meta_with_retry_desc_limit(meta)
        retry_info = {
            "triggered": True,
            "error": str(e),
            "original_units": bilibili_text_units(meta.desc),
            "retry_units": bilibili_text_units(retry_meta.desc),
            "retry_max_units": BILIBILI_DESC_RETRY_MAX_CHARS,
            "cleared_desc_v2": meta.desc_v2 is not None,
        }
        logger.warning(
            "bilibili desc too long; retrying with shorter desc (original_units=%s retry_units=%s retry_max_units=%s)",
            retry_info["original_units"],
            retry_info["retry_units"],
            retry_info["retry_max_units"],
        )
        return client.add_archive(retry_meta, csrf=csrf, tid=tid, uploaded=uploaded, cover_url=cover_url), retry_meta, retry_info


def _process_job_impl(self, job_id: str) -> dict[str, Any]:
    _ensure_db()
    db = _db()
    job_uuid: uuid.UUID | None = None
    job: PublishJob | None = None
    task: Task | None = None
    store: S3Store | None = None
    upload_throttle: dict[str, Any] | None = None
    submit_throttle: dict[str, Any] | None = None
    job_lock: Any = None
    try:
        job_uuid = uuid.UUID(job_id)
        job_lock = _try_acquire_publish_job_lock(job_id)
        if job_lock is False:
            return {"status": "skipped", "detail": "publish job already running"}

        job = db.get(PublishJob, job_uuid)
        if not job:
            return {"status": "not_found"}

        if job.state in (PublishState.published, PublishState.failed):
            return {"status": "skipped", "state": job.state.value}

        task = db.get(Task, job.task_id)
        if not task:
            job.state = PublishState.failed
            job.response_json = {"error": "task not found"}
            job.updated_at = _utcnow()
            db.add(job)
            db.commit()
            return {"status": "error", "detail": "task not found"}

        published_job = _latest_published_job(db, task.id, job.account_id)
        if published_job is not None or (job.batch_id is None and task.status == TaskStatus.published):
            _mirror_published_job(job, published_job, task)
            db.add(job)
            db.add(task)
            cleanup_enqueued = _reconcile_batch_after_publish_job(db, job)
            db.commit()
            if job.batch_id is not None:
                enqueue_publish_batch_cleanup(db, celery_app, task.id, job.batch_id, needed=cleanup_enqueued)
            return {"status": "skipped", "detail": "task already published", "aid": job.aid, "bvid": job.bvid}

        cookie = (get_bilibili_cookie_header(db) or "").strip()
        csrf = (get_bilibili_csrf_token(db) or "").strip()
        if not cookie:
            raise RuntimeError("bilibili cookie is not set")
        if not csrf:
            raise RuntimeError("bilibili csrf (bili_jct) is not set")

        meta_json = _as_dict(job.meta_json)
        # Backward compatible: earlier versions stored meta directly.
        meta_dict = _as_dict(meta_json.get("meta")) or meta_json
        meta = BilibiliPublishMeta.model_validate(meta_dict)
        typeid_mode = str(meta_json.get("typeid_mode") or "bilibili_predict").strip() or "bilibili_predict"

        video_key = _extract_video_key(meta_json)
        if not video_key:
            raise RuntimeError("publish job missing video key")

        store = S3Store(settings)
        store.ensure_bucket()

        job.state = PublishState.submitting
        job.updated_at = _utcnow()
        db.add(job)
        db.commit()

        with tempfile.TemporaryDirectory(prefix="videoroll_bili_") as td:
            workdir = Path(td)
            video_path = workdir / Path(video_key).name
            store.download_file(video_key, video_path)

            cover_url = ""
            cover_path: Path | None = None
            cover_key = str(job.cover_key or "").strip()
            if cover_key:
                cover_path = workdir / Path(cover_key).name
                store.download_file(cover_key, cover_path)

            collection_result: dict[str, Any] | None = None
            desc_retry: dict[str, Any] | None = None
            with BilibiliWebClient(cookie) as client:
                if cover_path:
                    cover_url = client.upload_cover(cover_path, csrf=csrf)

                upload_throttle = _apply_publish_stage_throttle(db, stage="upload", job_id=job_id)
                uploaded, upload_debug = client.upload_video_file(video_path)
                predicted_tid: int | None = None
                tid_meta = int(meta.typeid)
                tid = tid_meta
                selected_by = "meta"

                ai_info: dict[str, Any] = {
                    "ok": False,
                    "typeid": None,
                    "path": None,
                    "reason": "",
                    "text_source": "",
                    "text_chars": 0,
                    "candidate_count": 0,
                }
                if typeid_mode == "ai_summary":
                    summary = get_task_bilibili_summary(db, str(task.id))
                    text_for_ai = (summary or "").strip()
                    text_source = "summary"
                    if not text_for_ai:
                        text_for_ai = f"{meta.title}\n{meta.desc}".strip()
                        text_source = "meta"

                    translate_settings = get_translate_settings(db, get_subtitle_settings())
                    api_key = str(translate_settings.get("openai_api_key") or "").strip()
                    ai_info["text_source"] = text_source
                    ai_info["text_chars"] = len(text_for_ai)

                    if not api_key:
                        ai_info["reason"] = "openai api key not set"
                    elif not text_for_ai:
                        ai_info["reason"] = "text is empty"
                    else:
                        try:
                            pre = client.archive_pre()
                            data = _as_dict(pre.get("data"))
                            typelist = data.get("typelist")
                            options = flatten_typelist(typelist)
                            ai_info["candidate_count"] = len(options)
                            if not options:
                                ai_info["reason"] = "bilibili typelist is empty"
                            else:
                                id_to_path = {int(o.get("id") or 0): str(o.get("path") or "").strip() for o in options}
                                obj = _ai_service().recommend_typeid(
                                    text_for_ai,
                                    options=options,
                                )
                                tid_ai = int(obj.get("typeid") or 0)
                                ai_info["typeid"] = tid_ai or None
                                ai_info["reason"] = str(obj.get("reason") or "").strip()
                                if tid_ai in id_to_path:
                                    tid = tid_ai
                                    selected_by = "ai_summary"
                                    ai_info["ok"] = True
                                    ai_info["path"] = id_to_path.get(tid_ai) or None
                        except Exception as e:
                            ai_info["reason"] = f"ai failed: {type(e).__name__}"

                    if selected_by != "ai_summary":
                        try:
                            predicted_tid = client.predict_type(
                                csrf=csrf,
                                filename=uploaded.filename_no_suffix,
                                title=meta.title,
                                upload_id=uploaded.upload_id,
                            )
                        except Exception:
                            predicted_tid = None
                        if predicted_tid:
                            tid = int(predicted_tid)
                            selected_by = "bilibili_predict"
                elif typeid_mode == "bilibili_predict":
                    predicted_tid = client.predict_type(
                        csrf=csrf,
                        filename=uploaded.filename_no_suffix,
                        title=meta.title,
                        upload_id=uploaded.upload_id,
                    )
                    if predicted_tid:
                        tid = int(predicted_tid)
                        selected_by = "bilibili_predict"

                typeid_debug = {
                    "mode": typeid_mode,
                    "selected": tid,
                    "selected_by": selected_by,
                    "meta": tid_meta,
                    "predicted": predicted_tid,
                    "ai": ai_info,
                }
                logger.info(
                    "select tid (mode=%s selected=%s by=%s meta=%s predicted=%s ai_ok=%s ai_tid=%s)",
                    typeid_mode,
                    tid,
                    selected_by,
                    tid_meta,
                    predicted_tid,
                    bool(ai_info.get("ok")),
                    ai_info.get("typeid"),
                )

                submit_throttle = _apply_publish_stage_throttle(db, stage="submit", job_id=job_id)
                add_resp, meta, desc_retry = _add_archive_with_desc_retry(
                    client=client,
                    meta=meta,
                    csrf=csrf,
                    tid=tid,
                    uploaded=uploaded,
                    cover_url=cover_url,
                )
                add_data = _as_dict(add_resp.get("data"))
                try:
                    aid_int = int(add_data.get("aid") or 0)
                except Exception:
                    aid_int = 0
                if task.source_type.value == "youtube":
                    if aid_int > 0:
                        collection_result = _attach_youtube_uploader_collection(
                            client=client,
                            task=task,
                            meta=meta,
                            uploaded_cid=uploaded.cid,
                            aid=aid_int,
                            csrf=csrf,
                            cover_url=cover_url,
                            store=store,
                            db=db,
                        )
                    else:
                        collection_result = {
                            "ok": False,
                            "skipped": True,
                            "reason": "publish response missing aid; cannot attach collection",
                        }
                    if collection_result and not bool(collection_result.get("ok")) and not bool(collection_result.get("skipped")):
                        logger.warning("youtube uploader collection attach failed (task_id=%s): %s", task.id, collection_result)

        data = _as_dict(add_resp.get("data"))
        aid = data.get("aid")
        bvid = data.get("bvid")
        aid_str = str(aid) if aid is not None else ""
        bvid_str = str(bvid) if bvid is not None else ""
        if not aid_str and not bvid_str:
            raise RuntimeError(f"submit succeeded but missing aid/bvid: {data}")

        job.state = PublishState.published
        job.aid = aid_str or None
        job.bvid = bvid_str or None
        job.external_id = bvid_str or aid_str or None
        job.external_url = f"https://www.bilibili.com/video/{bvid_str}" if bvid_str else None
        job.response_json = {
            "mode": "web",
            "cover_url": cover_url or None,
            "video": upload_debug,
            "throttle": {
                "upload": upload_throttle,
                "submit": submit_throttle,
            },
            "tid": tid,
            "typeid": typeid_debug,
            "desc_retry": desc_retry,
            "result": {"aid": aid_str or None, "bvid": bvid_str or None},
            "collection": collection_result,
        }
        job.updated_at = _utcnow()
        db.add(job)

        cleanup_enqueued = False
        if job.batch_id is not None:
            cleanup_enqueued = _reconcile_batch_after_publish_job(db, job)
        else:
            task.status = TaskStatus.published
            task.error_code = None
            task.error_message = None
            db.add(task)

        result_key = unique_publish_result_key(task.id)
        result_bytes = json.dumps(job.response_json, ensure_ascii=False, indent=2).encode("utf-8")
        store.put_bytes(result_bytes, result_key, content_type="application/json")
        _ensure_publish_result_asset(db, task.id, result_key)

        db.commit()
        try:
            enqueue_publish_batch_cleanup(db, celery_app, task.id, job.batch_id, needed=cleanup_enqueued) if job.batch_id else None
        except Exception:
            logger.exception("failed to enqueue batch cleanup task (task_id=%s batch_id=%s)", task.id, job.batch_id)
        return {"status": "ok", "aid": aid_str or None, "bvid": bvid_str or None}
    except Retry:
        raise
    except BilibiliRateLimitError as e:
        retries_done = int(getattr(self.request, "retries", 0))
        max_retries = int(getattr(self, "max_retries", 0) or 0)
        stage = _rate_limit_stage(e)
        countdown = _rate_limit_retry_countdown(stage=stage, retries_done=retries_done)
        cooldown_info = _extend_publish_stage_cooldown(stage, delay_seconds=countdown)

        logger.warning(
            "bilibili rate limited (job_id=%s stage=%s code=%s status=%s v_voucher=%s retries=%s/%s countdown=%.3fs)",
            job_id,
            stage,
            e.code,
            e.status_code,
            (e.v_voucher or "-"),
            retries_done,
            max_retries,
            countdown,
        )
        try:
            if job_uuid is None:
                job_uuid = uuid.UUID(job_id)
            if job is None:
                job = db.get(PublishJob, job_uuid)
            if job:
                job.state = PublishState.submitting
                job.response_json = {
                    "error": str(e),
                    "exception_type": type(e).__name__,
                    "rate_limited": True,
                    "stage": stage,
                    "code": e.code,
                    "status_code": e.status_code,
                    "message": e.message,
                    "v_voucher": e.v_voucher,
                    "cooldown": cooldown_info,
                    "throttle": {
                        "upload": upload_throttle,
                        "submit": submit_throttle,
                    },
                    "attempt": retries_done + 1,
                    "max_attempts": max_retries + 1,
                    "retries_done": retries_done,
                    "max_retries": max_retries,
                    "retry_in_seconds": countdown,
                }
                job.updated_at = _utcnow()
                db.add(job)
            if task is None and job:
                task = db.get(Task, job.task_id)
            if job and job.batch_id is not None:
                _reconcile_batch_after_publish_job(db, job)
            elif task:
                task.status = TaskStatus.publishing
                db.add(task)
            if store is None:
                store = S3Store(settings)
                store.ensure_bucket()
            if store and task:
                result_key = unique_publish_result_key(task.id)
                result_bytes = json.dumps(job.response_json or {"error": str(e)}, ensure_ascii=False, indent=2).encode("utf-8")
                store.put_bytes(result_bytes, result_key, content_type="application/json")
                _ensure_publish_result_asset(db, task.id, result_key)
            db.commit()
        except Exception:
            pass

        # Celery's retry counter is 0-based and counts *retries*, so allow up to `max_retries`
        # (total attempts = max_retries + 1), same as biliup-master.
        if retries_done >= max_retries:
            # Give up and mark failed with a clear message.
            try:
                if job_uuid is None:
                    job_uuid = uuid.UUID(job_id)
                job = db.get(PublishJob, job_uuid)
                if job:
                    job.state = PublishState.failed
                    job.response_json = {
                        **_as_dict(job.response_json),
                        "give_up": True,
                        "error": str(e),
                        "exception_type": type(e).__name__,
                        "rate_limited": True,
                        "stage": stage,
                        "code": e.code,
                        "status_code": e.status_code,
                        "message": e.message,
                        "v_voucher": e.v_voucher,
                        "cooldown": cooldown_info,
                        "throttle": {
                            "upload": upload_throttle,
                            "submit": submit_throttle,
                        },
                        "attempt": retries_done + 1,
                        "max_attempts": max_retries + 1,
                        "retries_done": retries_done,
                        "max_retries": max_retries,
                    }
                    job.updated_at = _utcnow()
                    db.add(job)
                if task is None and job:
                    task = db.get(Task, job.task_id)
                cleanup_enqueued = False
                if job and job.batch_id is not None:
                    cleanup_enqueued = _reconcile_batch_after_publish_job(db, job)
                elif task and _can_mark_task_publish_failed(db, task):
                    task.status = TaskStatus.failed
                    task.error_code = "PUBLISH_RATE_LIMITED"
                    task.error_message = e.message or str(e)
                    db.add(task)
                if store is None:
                    store = S3Store(settings)
                    store.ensure_bucket()
                if store and task and job and job.response_json:
                    result_key = unique_publish_result_key(task.id)
                    store.put_bytes(
                        json.dumps(job.response_json, ensure_ascii=False, indent=2).encode("utf-8"),
                        result_key,
                        content_type="application/json",
                    )
                    _ensure_publish_result_asset(db, task.id, result_key)
                db.commit()
                if job and job.batch_id is not None:
                    enqueue_publish_batch_cleanup(db, celery_app, task.id, job.batch_id, needed=cleanup_enqueued)
            except Exception:
                pass
            return {"status": "error", "detail": "rate limited; max retries exceeded"}

        raise self.retry(exc=e, countdown=countdown)
    except Exception as e:
        logger.exception("bilibili publish failed (job_id=%s)", job_id)
        tb = traceback.format_exc(limit=30)
        try:
            if job_uuid is None:
                job_uuid = uuid.UUID(job_id)
            if job is None:
                job = db.get(PublishJob, job_uuid)
            if job:
                job.state = PublishState.failed
                job.response_json = {"error": str(e), "exception_type": type(e).__name__, "traceback": tb}
                job.updated_at = _utcnow()
                db.add(job)
            if task is None and job:
                task = db.get(Task, job.task_id)
            cleanup_enqueued = False
            if job and job.batch_id is not None:
                cleanup_enqueued = _reconcile_batch_after_publish_job(db, job)
            elif task and _can_mark_task_publish_failed(db, task):
                task.status = TaskStatus.failed
                task.error_code = "PUBLISH_FAILED"
                task.error_message = str(e)
                db.add(task)
            if store is None:
                store = S3Store(settings)
                store.ensure_bucket()
            if store and task:
                result_key = unique_publish_result_key(task.id)
                result_bytes = json.dumps(
                    {"error": str(e), "exception_type": type(e).__name__, "traceback": tb},
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
                store.put_bytes(result_bytes, result_key, content_type="application/json")
                _ensure_publish_result_asset(db, task.id, result_key)
            db.commit()
            if job and job.batch_id is not None:
                enqueue_publish_batch_cleanup(db, celery_app, task.id, job.batch_id, needed=cleanup_enqueued)
        except Exception:
            pass
        return {"status": "error", "detail": str(e)}
    finally:
        if job_lock not in (None, False):
            try:
                job_lock.release()
            except Exception:
                pass
        db.close()


@celery_app.task(name="bilibili_publisher.process_job", bind=True, max_retries=5)
def process_job(self, job_id: str, outbox_event_id: str | None = None) -> dict[str, Any]:
    """Consume durable events while preserving the legacy task name/arguments."""
    _ensure_db()
    if outbox_event_id is None:
        return _process_job_impl(self, job_id)

    owner = f"bilibili_publisher.process_job:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    claim_db = _db()
    try:
        claim = claim_outbox_operation(claim_db, outbox_event_id, owner, lease_seconds=1800)
        if claim is None:
            return {"status": "error", "detail": "outbox event not found"}
        if not claim.acquired:
            if claim.result_json is not None:
                return claim.result_json
            return {"status": "in_progress", "operation_key": claim.operation.operation_key}
        operation_key = claim.operation.operation_key
        claim_db.commit()
    finally:
        claim_db.close()

    heartbeat = OperationHeartbeat(lambda: _db(), operation_key, owner, lease_seconds=1800)
    heartbeat.start()
    try:
        result = _process_job_impl(self, job_id)
    except Retry as exc:
        retry_db = _db()
        try:
            release_operation(retry_db, operation_key, owner, exc)
            retry_db.commit()
        finally:
            retry_db.close()
        raise
    finally:
        heartbeat.stop()

    finish_db = _db()
    try:
        finish_operation(finish_db, operation_key, result)
        finish_db.commit()
    finally:
        finish_db.close()
    return result
