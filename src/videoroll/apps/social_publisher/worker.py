from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import redis
from celery import Celery
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from videoroll.apps.social_publisher.runtime import materialized_account_state, social_lock_key
from videoroll.apps.social_publisher.sau_cli import (
    SauCommandResult,
    build_check_command,
    build_upload_video_command,
    run_sau_command,
)
from videoroll.config import get_social_publisher_settings
from videoroll.db.auto_migrate import auto_migrate
from videoroll.db.models import Account, Platform, PublishJob, PublishState, Task, TaskStatus
from videoroll.db.session import db_session
from videoroll.storage.s3 import S3Store


settings = get_social_publisher_settings()
logger = logging.getLogger(__name__)
celery_app = Celery("social_publisher", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.beat_schedule = {
    "social-publisher-mark-stale": {
        "task": "social_publisher.mark_stale_jobs_unknown",
        "schedule": 60.0,
    }
}


def _db() -> Session:
    return next(db_session(settings.database_url))


def _ensure_db() -> None:
    auto_migrate(settings.database_url)


def _redis_client():
    return redis.Redis.from_url(settings.redis_url)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_message(value: object, limit: int = 1000) -> str:
    message = str(value or "").replace("\x00", "").strip()
    return message[:limit]


def classify_execution_result(*, returncode: int, timed_out: bool) -> PublishState:
    return PublishState.submitted if not timed_out and returncode == 0 else PublishState.unknown


def account_check_message(result: SauCommandResult) -> str:
    if result.timed_out:
        return f"SAU check timed out (exit={result.returncode})"
    if result.returncode == 0:
        return "cookie valid"
    detail = _clean_message(result.stderr or result.stdout, limit=700)
    prefix = f"SAU check failed (exit={result.returncode})"
    return f"{prefix}: {detail}" if detail else prefix


def _mark_task_error(task: Task | None, code: str, message: str) -> None:
    if task is None or task.status in {TaskStatus.published, TaskStatus.canceled}:
        return
    task.error_code = code
    task.error_message = _clean_message(message)
    if code == "SOCIAL_PUBLISH_FAILED":
        task.status = TaskStatus.failed


@celery_app.task(name="social_publisher.check_account", bind=True, max_retries=20)
def check_account(self, account_id: str) -> dict[str, Any]:
    _ensure_db()
    db = _db()
    lock = None
    try:
        account = db.get(Account, uuid.UUID(account_id))
        if not account or account.platform not in {Platform.douyin, Platform.xiaohongshu, Platform.kuaishou}:
            return {"status": "not_found"}
        account.check_state = "checking"
        db.add(account)
        db.commit()
        lock = _redis_client().lock(
            social_lock_key(account.platform.value, account.id),
            timeout=int(settings.account_check_timeout_seconds + settings.lock_margin_seconds),
            blocking_timeout=0,
        )
        if not lock.acquire(blocking=False):
            account.check_state = "queued"
            account.last_check_message = "account is busy"
            db.add(account)
            db.commit()
            raise self.retry(countdown=10)
        with materialized_account_state(account, settings):
            command = build_check_command(settings, account.platform.value, account.name)
            logger.info("running SAU account check platform=%s account=%s command=%r", account.platform.value, account.name, command)
            result = run_sau_command(settings, command, timeout_seconds=settings.account_check_timeout_seconds)
        logger.info(
            "SAU account check completed platform=%s account=%s exit=%s timed_out=%s stdout=%r stderr=%r",
            account.platform.value,
            account.name,
            result.returncode,
            result.timed_out,
            _clean_message(result.stdout, limit=1000),
            _clean_message(result.stderr, limit=1000),
        )
        account.check_state = "valid" if result.returncode == 0 and not result.timed_out else "invalid"
        account.last_checked_at = datetime.now(timezone.utc)
        account.last_check_message = account_check_message(result)
        db.add(account)
        db.commit()
        return {"status": account.check_state}
    except Retry:
        raise
    except Exception as exc:
        logger.exception("SAU account check crashed account_id=%s", account_id)
        db.rollback()
        account = db.get(Account, uuid.UUID(account_id))
        if account:
            account.check_state = "error"
            account.last_checked_at = datetime.now(timezone.utc)
            account.last_check_message = _clean_message(exc)
            db.add(account)
            db.commit()
        return {"status": "error", "detail": _clean_message(exc)}
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        db.close()


@celery_app.task(name="social_publisher.process_job", bind=True, max_retries=20)
def process_job(self, job_id: str) -> dict[str, Any]:
    _ensure_db()
    db = _db()
    lock = None
    job: PublishJob | None = None
    task: Task | None = None
    work_dir: Path | None = None
    try:
        job = db.get(PublishJob, uuid.UUID(job_id))
        if not job:
            return {"status": "not_found"}
        if job.state in {PublishState.submitted, PublishState.published, PublishState.unknown, PublishState.failed}:
            return {"status": "skipped", "state": job.state.value}
        task = db.get(Task, job.task_id)
        account = db.get(Account, job.account_id) if job.account_id else None
        if not account or not account.is_active:
            raise ValueError("active social account not found")
        if account.check_state != "valid":
            raise ValueError("social account is not validated")
        lock = _redis_client().lock(
            social_lock_key(job.platform.value, account.id),
            timeout=int(settings.upload_timeout_seconds + settings.lock_margin_seconds),
            blocking_timeout=0,
        )
        if not lock.acquire(blocking=False):
            raise self.retry(countdown=30)

        request = _as_dict(job.meta_json)
        video = _as_dict(request.get("video"))
        cover = _as_dict(request.get("cover"))
        meta = _as_dict(request.get("meta"))
        platform_options = _as_dict(request.get("platform_options"))
        video_key = str(video.get("key") or "").strip()
        if not video_key:
            raise ValueError("video S3 key is missing")
        work_dir = Path(settings.work_dir) / str(job.id)
        work_dir.mkdir(parents=True, exist_ok=True)
        video_path = work_dir / (Path(video_key).name or "video.mp4")
        cover_key = str(cover.get("key") or "").strip()
        cover_path = work_dir / (Path(cover_key).name or "cover.jpg") if cover_key else None
        store = S3Store(settings)
        store.download_file(video_key, video_path)
        if cover_path is not None:
            store.download_file(cover_key, cover_path)

        with materialized_account_state(account, settings):
            command = build_upload_video_command(
                settings,
                platform=job.platform.value,
                account_name=account.name,
                video_path=video_path,
                cover_path=cover_path,
                meta=meta,
                platform_options=platform_options,
            )
            job.started_at = datetime.now(timezone.utc)
            job.state = PublishState.submitting
            db.add(job)
            db.commit()
            result = run_sau_command(settings, command, timeout_seconds=settings.upload_timeout_seconds)
        db.add(account)

        job.state = classify_execution_result(returncode=result.returncode, timed_out=result.timed_out)
        job.finished_at = datetime.now(timezone.utc)
        job.response_json = {
            "driver": "sau",
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if task and task.status not in {TaskStatus.published, TaskStatus.canceled}:
            task.status = TaskStatus.publishing
            task.error_code = None if job.state == PublishState.submitted else "SOCIAL_PUBLISH_UNKNOWN"
            task.error_message = None if job.state == PublishState.submitted else "social publish result is unknown; check platform backend before retrying"
            db.add(task)
        db.add(job)
        db.commit()
        return {"status": job.state.value, "job_id": str(job.id)}
    except Retry:
        raise
    except Exception as exc:
        db.rollback()
        if job is not None:
            started = job.started_at is not None
            job.state = PublishState.unknown if started else PublishState.failed
            job.finished_at = datetime.now(timezone.utc)
            job.response_json = {"error": _clean_message(exc), "started": started}
            _mark_task_error(task, "SOCIAL_PUBLISH_UNKNOWN" if started else "SOCIAL_PUBLISH_FAILED", _clean_message(exc))
            db.add(job)
            if task is not None:
                db.add(task)
            db.commit()
            return {"status": job.state.value, "detail": _clean_message(exc)}
        return {"status": "error", "detail": _clean_message(exc)}
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        db.close()


@celery_app.task(name="social_publisher.mark_stale_jobs_unknown")
def mark_stale_jobs_unknown() -> dict[str, int]:
    _ensure_db()
    db = _db()
    try:
        now = datetime.now(timezone.utc)
        running_cutoff = now - timedelta(seconds=settings.upload_timeout_seconds + settings.lock_margin_seconds)
        queued_cutoff = now - timedelta(minutes=30)
        running = (
            db.query(PublishJob)
            .filter(
                PublishJob.platform.in_([Platform.douyin, Platform.xiaohongshu, Platform.kuaishou]),
                PublishJob.state == PublishState.submitting,
                PublishJob.started_at.is_not(None),
                PublishJob.started_at < running_cutoff,
            )
            .all()
        )
        queued = (
            db.query(PublishJob)
            .filter(
                PublishJob.platform.in_([Platform.douyin, Platform.xiaohongshu, Platform.kuaishou]),
                PublishJob.state == PublishState.submitting,
                PublishJob.started_at.is_(None),
                PublishJob.created_at < queued_cutoff,
            )
            .all()
        )
        for job in running:
            job.state = PublishState.unknown
            job.finished_at = now
            job.response_json = {**_as_dict(job.response_json), "error": "worker execution timed out"}
            db.add(job)
            task = db.get(Task, job.task_id)
            _mark_task_error(task, "SOCIAL_PUBLISH_UNKNOWN", "worker execution timed out; check platform backend before retrying")
            if task is not None:
                db.add(task)
        for job in queued:
            job.state = PublishState.failed
            job.finished_at = now
            job.response_json = {**_as_dict(job.response_json), "error": "job expired before browser execution"}
            db.add(job)
            task = db.get(Task, job.task_id)
            _mark_task_error(task, "SOCIAL_PUBLISH_FAILED", "job expired before browser execution")
            if task is not None:
                db.add(task)
        db.commit()
        return {"unknown": len(running), "failed": len(queued)}
    finally:
        db.close()
