from __future__ import annotations

import json
import logging
import random
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celery import Celery
from sqlalchemy.orm import Session

from videoroll.apps.bilibili_publisher.auth_settings_store import get_bilibili_cookie_header, get_bilibili_csrf_token
from celery.exceptions import Retry

from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliRateLimitError, BilibiliWebClient
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta
from videoroll.apps.bilibili_publisher.typeid_recommender import flatten_typelist, recommend_typeid_openai
from videoroll.config import get_bilibili_publisher_settings, get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, PublishJob, PublishState, Task, TaskStatus
from videoroll.db.session import get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_summary
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings


settings = get_bilibili_publisher_settings()
logger = logging.getLogger(__name__)
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


def _ensure_db() -> None:
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


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


@celery_app.task(name="bilibili_publisher.process_job", bind=True, max_retries=5)
def process_job(self, job_id: str) -> dict[str, Any]:
    _ensure_db()
    db = _db()
    job_uuid: uuid.UUID | None = None
    job: PublishJob | None = None
    task: Task | None = None
    store: S3Store | None = None
    try:
        job_uuid = uuid.UUID(job_id)
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

            with BilibiliWebClient(cookie) as client:
                if cover_path:
                    cover_url = client.upload_cover(cover_path, csrf=csrf)

                uploaded, upload_debug = client.upload_video_file(video_path)
                predicted_tid: int | None = None
                if typeid_mode == "bilibili_predict":
                    predicted_tid = client.predict_type(
                        csrf=csrf,
                        filename=uploaded.filename_no_suffix,
                        title=meta.title,
                        upload_id=uploaded.upload_id,
                    )

                tid = int(meta.typeid)
                if typeid_mode == "ai_summary":
                    ai_ok = False
                    summary = get_task_bilibili_summary(db, str(task.id))
                    translate_settings = get_translate_settings(db, get_subtitle_settings())
                    if summary and translate_settings.get("openai_api_key"):
                        try:
                            pre = client.archive_pre()
                            data = _as_dict(pre.get("data"))
                            typelist = data.get("typelist")
                            options = flatten_typelist(typelist)
                            obj = recommend_typeid_openai(
                                summary,
                                options=options,
                                api_key=str(translate_settings.get("openai_api_key") or ""),
                                base_url=str(translate_settings.get("openai_base_url") or ""),
                                model=str(translate_settings.get("openai_model") or ""),
                                temperature=float(translate_settings.get("openai_temperature") or 0.0),
                                timeout_seconds=float(translate_settings.get("openai_timeout_seconds") or 30.0),
                            )
                            tid_ai = int(obj.get("typeid") or 0)
                            candidate_ids = {int(o.get("id") or 0) for o in options}
                            if tid_ai in candidate_ids:
                                tid = tid_ai
                                ai_ok = True
                        except Exception:
                            pass

                    if not ai_ok and predicted_tid is None:
                        try:
                            predicted_tid = client.predict_type(
                                csrf=csrf,
                                filename=uploaded.filename_no_suffix,
                                title=meta.title,
                                upload_id=uploaded.upload_id,
                            )
                        except Exception:
                            predicted_tid = None
                    if not ai_ok and predicted_tid:
                        tid = int(predicted_tid)
                elif typeid_mode == "bilibili_predict":
                    if predicted_tid:
                        tid = int(predicted_tid)

                add_resp = client.add_archive(meta, csrf=csrf, tid=tid, uploaded=uploaded, cover_url=cover_url)

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
        job.response_json = {
            "mode": "web",
            "cover_url": cover_url or None,
            "video": upload_debug,
            "tid": tid,
            "result": {"aid": aid_str or None, "bvid": bvid_str or None},
        }
        job.updated_at = _utcnow()
        db.add(job)

        task.status = TaskStatus.published
        task.error_code = None
        task.error_message = None
        db.add(task)

        result_key = f"meta/{task.id}/publish_result.json"
        result_bytes = json.dumps(job.response_json, ensure_ascii=False, indent=2).encode("utf-8")
        store.put_bytes(result_bytes, result_key, content_type="application/json")
        _ensure_publish_result_asset(db, task.id, result_key)

        db.commit()
        return {"status": "ok", "aid": aid_str or None, "bvid": bvid_str or None}
    except Retry:
        raise
    except BilibiliRateLimitError as e:
        retries_done = int(getattr(self.request, "retries", 0))
        max_retries = int(getattr(self, "max_retries", 0) or 0)

        logger.warning(
            "bilibili rate limited (job_id=%s code=%s status=%s v_voucher=%s retries=%s/%s)",
            job_id,
            e.code,
            e.status_code,
            (e.v_voucher or "-"),
            retries_done,
            max_retries,
        )

        # Match biliup-master: exponential backoff + jitter, cap 64s.
        countdown = min(64.0, (2 ** (retries_done + 1)) + random.random())
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
                    "code": e.code,
                    "status_code": e.status_code,
                    "message": e.message,
                    "v_voucher": e.v_voucher,
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
            if task:
                task.status = TaskStatus.publishing
                db.add(task)
            if store is None:
                store = S3Store(settings)
                store.ensure_bucket()
            if store and task:
                result_key = f"meta/{task.id}/publish_result.json"
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
                        "code": e.code,
                        "status_code": e.status_code,
                        "message": e.message,
                        "v_voucher": e.v_voucher,
                        "attempt": retries_done + 1,
                        "max_attempts": max_retries + 1,
                        "retries_done": retries_done,
                        "max_retries": max_retries,
                    }
                    job.updated_at = _utcnow()
                    db.add(job)
                if task is None and job:
                    task = db.get(Task, job.task_id)
                if task:
                    task.status = TaskStatus.failed
                    task.error_code = "PUBLISH_RATE_LIMITED"
                    task.error_message = e.message or str(e)
                    db.add(task)
                if store is None:
                    store = S3Store(settings)
                    store.ensure_bucket()
                if store and task and job and job.response_json:
                    result_key = f"meta/{task.id}/publish_result.json"
                    store.put_bytes(
                        json.dumps(job.response_json, ensure_ascii=False, indent=2).encode("utf-8"),
                        result_key,
                        content_type="application/json",
                    )
                    _ensure_publish_result_asset(db, task.id, result_key)
                db.commit()
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
            if task:
                task.status = TaskStatus.failed
                task.error_code = "PUBLISH_FAILED"
                task.error_message = str(e)
                db.add(task)
            if store is None:
                store = S3Store(settings)
                store.ensure_bucket()
            if store and task:
                result_key = f"meta/{task.id}/publish_result.json"
                result_bytes = json.dumps(
                    {"error": str(e), "exception_type": type(e).__name__, "traceback": tb},
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
                store.put_bytes(result_bytes, result_key, content_type="application/json")
                _ensure_publish_result_asset(db, task.id, result_key)
            db.commit()
        except Exception:
            pass
        return {"status": "error", "detail": str(e)}
    finally:
        db.close()
