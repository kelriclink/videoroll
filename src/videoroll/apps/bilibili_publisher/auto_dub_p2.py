"""Append native YouDub as a *second part of the same published Bilibili aid*.

The original P1 publish is irreversible and remains successful regardless of
what happens here. A durable publish outbox event starts this independent flow.
A persisted 'submitting' state prevents replaying an ambiguous archive edit.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from celery import Celery
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from videoroll.apps.dubbing.job import create_dubbing_job
from videoroll.apps.dubbing import youdub_control
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile, normalize_auto_dub_prefix
from videoroll.apps.outbox.service import create_outbox_event
from videoroll.db.models import (
    Asset, AssetKind, DubbingJob, Platform, PublishJob, PublishState,
    SubtitleJob, SubtitleJobStatus, Task,
)
from videoroll.utils.auto_youtube import parse_auto_youtube_created_by

logger = logging.getLogger(__name__)
EVENT_NAME = "bilibili.auto_dub_p2"
TASK_NAME = "bilibili_publisher.auto_dub_p2"
STORAGE_KEY = "auto_dub_p2"


def dual_part_archive_title(original: str, original_prefix: str) -> str:
    """Combined title for the Bilibili *archive*, not P1/P2 part titles.

    Only the auto dubbing branch calls this helper. Preserve the remaining
    title, including its uploader suffix, and avoid repeated prefixes.
    """
    from videoroll.apps.publish_meta_rules import clamp_text

    original = str(original or "").strip()
    prefix = "【熟肉&中配】"
    if original.startswith(prefix):
        return clamp_text(original, 80)
    old = str(original_prefix or "").strip()
    if old and original.startswith(old):
        original = original[len(old):].lstrip()
    elif original.startswith("【熟肉】"):
        original = original[len("【熟肉】"):].lstrip()
    return clamp_text(prefix + original, 80)


def p2_title(original: str, original_prefix: str, dubbed_prefix: str) -> str:
    """Replace only the auto-publish marker, leaving the rest of P1 title intact."""
    original = str(original or "").strip()
    source = str(original_prefix or "").strip()
    marker = normalize_auto_dub_prefix(dubbed_prefix)
    if source and original.startswith(source):
        original = original[len(source):].lstrip()
    # Never insert an unrelated publishing prefix into a P2 part title.
    if not original:
        original = "中文配音"
    result = marker + original
    if len(result) > 80:
        result = result[:80]
    return result


def schedule_after_p1(db: Session, job: PublishJob, task: Task) -> bool:
    """Run inside the *same transaction* recording successful P1 publication."""
    if job.platform != Platform.bilibili or job.state != PublishState.published or not job.aid:
        return False
    if job.batch_id is None or parse_auto_youtube_created_by(task.created_by) is None:
        return False
    profile = get_auto_profile(db)
    if not (
        bool(profile.get("auto_publish")) and bool(profile.get("auto_dub_p2"))
        and "bilibili" in (profile.get("auto_publish_platforms") or [])
    ):
        return False
    recorded = dict(job.response_json or {})
    if STORAGE_KEY in recorded:
        return False
    original = str((job.meta_json or {}).get("meta", {}).get("title") or
                   (job.meta_json or {}).get("title") or "").strip()
    if not original:
        return False
    p2 = {
        "state": "queued",
        "prefix": normalize_auto_dub_prefix(profile.get("auto_dub_p2_prefix")),
        "title": p2_title(original, str(profile.get("publish_title_prefix") or ""), str(profile.get("auto_dub_p2_prefix") or "")),
        "dub_job_id": None, "dispatched": False, "error": None,
    }
    job.response_json = {**recorded, STORAGE_KEY: p2}
    db.add(job)
    create_outbox_event(
        db, event_type=EVENT_NAME, aggregate_type="publish_job", aggregate_id=job.id,
        task_name=TASK_NAME, args={"args": [str(job.id)], "queue": "publish"},
        operation_key=f"bilibili:auto-dub-p2:{job.id}",
    )
    return True


def _load_locked(db: Session, job_id: uuid.UUID) -> tuple[PublishJob, dict[str, Any]]:
    job = db.query(PublishJob).filter(PublishJob.id == job_id).with_for_update().one()
    record = dict((job.response_json or {}).get(STORAGE_KEY) or {})
    if job.state != PublishState.published or job.platform != Platform.bilibili or not job.aid or not record:
        raise RuntimeError("P1 publish is not confirmed or P2 was not scheduled")
    return job, record


def _save(db: Session, job: PublishJob, record: dict[str, Any]) -> None:
    job.response_json = {**dict(job.response_json or {}), STORAGE_KEY: dict(record)}
    db.add(job)
    db.commit()


def _source(db: Session, task_id: uuid.UUID) -> tuple[SubtitleJob, Asset]:
    subtitle = (db.query(SubtitleJob)
        .filter(SubtitleJob.task_id == task_id, SubtitleJob.status == SubtitleJobStatus.succeeded)
        .order_by(SubtitleJob.created_at.desc()).first())
    video = (db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_raw)
        .order_by(Asset.created_at.desc()).first())
    if subtitle is None or video is None:
        raise RuntimeError("P2 source: successful Chinese SRT or original video is missing")
    if not (subtitle.request_json or {}).get("artifacts", {}).get("final_subtitle_segments_key"):
        raise RuntimeError("P2 source: translated subtitle JSON is missing")
    return subtitle, video


def _start(db: Session, publish_id: uuid.UUID) -> tuple[str, uuid.UUID]:
    """Persist the native job ID before dispatch; re-entry reuses it."""
    job, record = _load_locked(db, publish_id)
    if record["state"] not in ("queued", "dubbing"):
        return str(record["state"]), uuid.UUID(str(record["dub_job_id"])) if record.get("dub_job_id") else uuid.UUID(int=0)
    if not youdub_control.status(db)["accepting_jobs"]:
        raise RuntimeError("YouDub is disabled or unavailable; enable the native engine")
    subtitle, video = _source(db, job.task_id)
    native = create_dubbing_job(
        db, task_id=job.task_id, subtitle_job_id=subtitle.id,
        subtitle_video_key=video.storage_key,
        operation_key=f"auto-dub-p2:{job.id}",
    )
    job, record = _load_locked(db, publish_id)
    record["dub_job_id"] = str(native.id)
    record["state"] = "dubbing"
    record.setdefault("dispatched", False)
    _save(db, job, record)
    return "dubbing", native.id


def _upload_then_append(db: Session, publish_id: uuid.UUID, *, client: Any, store: Any, csrf: str) -> dict[str, Any]:
    """Uploads P2 once; never retries an uncertain Bilibili archive edit."""
    job, state = _load_locked(db, publish_id)
    # Only one consumer can claim the upload. A crashed upload stays
    # "uploading" for operator reconciliation, never another automatic edit.
    if state["state"] in ("published", "submitting", "uploading", "unknown"):
        return state
    dub_id = uuid.UUID(str(state["dub_job_id"]))
    dub = db.get(DubbingJob, dub_id)
    if dub is None or dub.status != "succeeded":
        raise RuntimeError("YouDub has not completed successfully")
    video_key = str((dub.output_json or {}).get("video_key") or "")
    verified = db.query(Asset).filter(
        Asset.task_id == job.task_id, Asset.kind == AssetKind.video_dubbed,
        Asset.storage_key == video_key,
    ).first()
    if not video_key or verified is None:
        raise RuntimeError("Native YouDub P2 video asset is not registered")
    aid = int(job.aid)
    title = str(state["title"])
    state["state"] = "uploading"
    # Claim the only uploader while the row is locked, then release the
    # PostgreSQL connection before any Bilibili network calls.
    _save(db, job, state)
    # Check the actual author's archive BEFORE an upload. If a previous edit
    # was accepted, never add the same P2 again.
    with client:
        archive = client.get_owned_archive(aid=aid)
        parts = archive["videos"]
        if any(p.get("title") == title for p in parts):
            state.update(state="published", aid=str(aid), recovered=True, error=None)
            _save(db, job, state)
            return state
        if len(parts) != 1:
            raise RuntimeError("P1 now has other parts; refusing to overwrite or create P3")
        previous = state.get("upload")
        if previous:
            from videoroll.apps.bilibili_publisher.bilibili_web_client import UploadedVideo
            uploaded = UploadedVideo(**previous)
        else:
            uploaded, debug = client.upload_video_file(store.path_for(video_key))
            state["upload"] = {
                "filename_no_suffix": uploaded.filename_no_suffix,
                "cid": uploaded.cid, "upload_id": uploaded.upload_id,
                "upos_uri": uploaded.upos_uri,
            }
            state["state"] = "uploaded"
            state["upload_debug"] = debug
            _save(db, job, state)
        # The sole dangerous network boundary: mark before issuing edit.
        state["state"] = "submitting"
        state["error"] = None
        _save(db, job, state)
        try:
            client.append_archive_part(archive=archive, aid=aid, uploaded=uploaded,
                                       title=title, csrf=csrf)
        except Exception as exc:
            # The edit may have succeeded before an HTTP timeout. Reconcile
            # manually or read owner part list, but NEVER submit twice.
            state.update(state="unknown", error=f"{type(exc).__name__}: {exc}"[:500])
            _save(db, job, state)
            return state
        state.update(state="published", aid=str(aid), cid=uploaded.cid, error=None)
        _save(db, job, state)
        return state


def register_auto_dub_p2_task(app: Celery) -> Any:
    @app.task(name=TASK_NAME, bind=True, max_retries=1000)
    def process(self: Any, publish_job_id: str) -> dict[str, Any]:
        # Import at run time: a publisher worker should not load the TTS model.
        from videoroll.apps.bilibili_publisher.auth_settings_store import (
            get_bilibili_cookie_header, get_bilibili_csrf_token,
        )
        from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient
        from videoroll.config import get_orchestrator_settings, get_bilibili_publisher_settings
        from videoroll.db.session import get_sessionmaker
        from videoroll.storage.filesystem import FileStore
        from videoroll.workflows.launcher import PipelineLauncher

        uid = uuid.UUID(publish_job_id)
        settings = get_bilibili_publisher_settings()
        db = get_sessionmaker(settings.database_url)()
        try:
            job, record = _load_locked(db, uid)
            current = record["state"]
            if current in ("published", "unknown", "failed", "submitting"):
                return record
            if not record.get("dub_job_id"):
                state, dub_id = _start(db, uid)
                if state != "dubbing":
                    return record
            else:
                dub_id = uuid.UUID(str(record["dub_job_id"]))
            job, record = _load_locked(db, uid)
            if not record.get("dispatched"):
                db.commit()  # release row lock before Hatchet HTTP call
                try:
                    PipelineLauncher(get_orchestrator_settings()).launch_dubbing_job(
                        dub_id, dispatch_key=f"auto-dub-p2:{dub_id}")
                except Exception as exc:
                    logger.warning("auto P2 native job dispatch pending: %s", type(exc).__name__)
                    raise self.retry(countdown=90) from exc
                job, record = _load_locked(db, uid)
                record["dispatched"] = True
                _save(db, job, record)
            db.expire_all()
            job, record = _load_locked(db, uid)
            dub = db.get(DubbingJob, uuid.UUID(str(record["dub_job_id"])))
            if dub is None:
                raise RuntimeError("native P2 job record disappeared")
            if dub.status in ("queued", "preparing", "synthesizing", "aligning",
                              "mixing", "rendering", "pause_requested"):
                db.commit()
                raise self.retry(countdown=90)
            if dub.status != "succeeded":
                record.update(state="failed", error=f"YouDub did not complete: {dub.status}: {dub.error_message}"[:500])
                _save(db, job, record)
                return record
            cookie = get_bilibili_cookie_header(db)
            csrf = get_bilibili_csrf_token(db)
            if not cookie or not csrf:
                record.update(state="failed", error="Bilibili authentication is unavailable")
                _save(db, job, record)
                return record
            db.commit()
            return _upload_then_append(
                db, uid, client=BilibiliWebClient(cookie), store=FileStore(settings), csrf=csrf,
            )
        except Retry:
            raise
        except Exception as exc:
            db.rollback()
            try:
                job, record = _load_locked(db, uid)
                if record.get("state") not in ("published", "unknown", "submitting"):
                    record.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:500])
                    _save(db, job, record)
            except Exception:
                logger.exception("unable to persist P2 automation failure (publish job=%s)", uid)
            logger.exception("automatic P2 failed without modifying P1 (publish job=%s)", uid)
            return {"status": "failed", "error": type(exc).__name__}
        finally:
            db.close()
    return process
