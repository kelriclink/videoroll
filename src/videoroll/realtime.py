from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from redis import Redis
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)

UI_EVENT_CHANNEL = "videoroll:ui-events:v1"
UI_EVENT_VERSION = 1
UI_EVENT_MAX_BYTES = 64 * 1024

_CLIENT_LOCK = threading.Lock()
_CLIENT_PID: int | None = None
_CLIENT: Redis | None = None
_LAST_WARNING_AT = 0.0
_SESSION_EVENTS_INSTALLED = False


def _redis_client(redis_url: str) -> Redis:
    global _CLIENT_PID, _CLIENT
    pid = os.getpid()
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_PID != pid:
            _CLIENT = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
                health_check_interval=30,
            )
            _CLIENT_PID = pid
        return _CLIENT


def _warn_publish_failure(exc: Exception) -> None:
    global _LAST_WARNING_AT
    now = time.monotonic()
    with _CLIENT_LOCK:
        if now - _LAST_WARNING_AT < 30.0:
            return
        _LAST_WARNING_AT = now
    logger.warning("realtime event publish failed: %s", exc)


def publish_ui_event(
    redis_url: str,
    *,
    topics: Iterable[str],
    name: str,
    data: dict[str, Any] | None = None,
    entity_id: str | uuid.UUID | None = None,
) -> bool:
    """Best-effort UI notification; database state remains authoritative."""
    normalized_topics = sorted({str(topic or "").strip() for topic in topics if str(topic or "").strip()})
    if not normalized_topics:
        return False
    event = {
        "v": UI_EVENT_VERSION,
        "type": "event",
        "event_id": str(uuid.uuid4()),
        "topics": normalized_topics,
        "name": str(name or "").strip()[:128],
        "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
        "entity_id": str(entity_id) if entity_id is not None else None,
        "data": data or {},
    }
    raw = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(raw) > UI_EVENT_MAX_BYTES:
        _warn_publish_failure(ValueError(f"event too large: {len(raw)} bytes"))
        return False
    try:
        _redis_client(redis_url).publish(UI_EVENT_CHANNEL, raw)
        return True
    except (RedisError, OSError, ValueError) as exc:
        _warn_publish_failure(exc)
        return False


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def publish_task_updated(redis_url: str, task: Any) -> bool:
    task_id = str(getattr(task, "id", "") or "")
    if not task_id:
        return False
    data = {
        "id": task_id,
        "source_type": _enum_value(getattr(task, "source_type", "")),
        "source_url": getattr(task, "source_url", None),
        "source_license": _enum_value(getattr(task, "source_license", "")),
        "source_proof_url": getattr(task, "source_proof_url", None),
        "status": _enum_value(getattr(task, "status", "")),
        "priority": int(getattr(task, "priority", 0) or 0),
        "created_by": getattr(task, "created_by", None),
        "error_code": getattr(task, "error_code", None),
        "error_message": getattr(task, "error_message", None),
        "retry_count": int(getattr(task, "retry_count", 0) or 0),
        "created_at": getattr(task, "created_at", None),
        "updated_at": getattr(task, "updated_at", None),
    }
    return publish_ui_event(
        redis_url,
        topics=["tasks", f"task:{task_id}"],
        name="task.updated",
        entity_id=task_id,
        data=data,
    )


def publish_job_updated(redis_url: str, job: Any, *, kind: str) -> bool:
    task_id = str(getattr(job, "task_id", "") or "")
    job_id = str(getattr(job, "id", "") or "")
    if not task_id or not job_id:
        return False
    if kind == "subtitle":
        data = {
            "id": job_id,
            "task_id": task_id,
            "status": _enum_value(getattr(job, "status", "")),
            "progress": int(getattr(job, "progress", 0) or 0),
            "error_message": getattr(job, "error_message", None),
            "created_at": getattr(job, "created_at", None),
            "updated_at": getattr(job, "updated_at", None),
        }
        name = "subtitle_job.updated"
    elif kind == "render":
        data = {
            "id": job_id,
            "task_id": task_id,
            "subtitle_job_id": str(getattr(job, "subtitle_job_id", "") or "") or None,
            "status": _enum_value(getattr(job, "status", "")),
            "progress": int(getattr(job, "progress", 0) or 0),
            "error_message": getattr(job, "error_message", None),
            "created_at": getattr(job, "created_at", None),
            "updated_at": getattr(job, "updated_at", None),
        }
        name = "render_job.updated"
    elif kind == "publish":
        data = {
            "id": job_id,
            "task_id": task_id,
            "batch_id": str(getattr(job, "batch_id", "") or "") or None,
            "platform": _enum_value(getattr(job, "platform", "bilibili")),
            "state": _enum_value(getattr(job, "state", "")),
            "aid": getattr(job, "aid", None),
            "bvid": getattr(job, "bvid", None),
            "external_id": getattr(job, "external_id", None),
            "external_url": getattr(job, "external_url", None),
            "account_id": str(getattr(job, "account_id", "") or "") or None,
            "upload_progress": int(getattr(job, "upload_progress", 0) or 0),
            "upload_active": bool(getattr(job, "upload_active", False)),
            "started_at": getattr(job, "started_at", None),
            "finished_at": getattr(job, "finished_at", None),
            "created_at": getattr(job, "created_at", None),
            "updated_at": getattr(job, "updated_at", None),
        }
        name = "publish_job.updated"
    else:
        raise ValueError(f"unsupported realtime job kind: {kind}")
    topics = ["tasks", "queue", f"task:{task_id}"]
    if kind == "publish":
        topics.append("publishing")
    return publish_ui_event(redis_url, topics=topics, name=name, entity_id=job_id, data=data)


def publish_queue_changed(redis_url: str, *, task_id: str | uuid.UUID | None = None) -> bool:
    topics = ["queue"]
    if task_id:
        topics.append(f"task:{task_id}")
    return publish_ui_event(
        redis_url,
        topics=topics,
        name="task_queue.changed",
        entity_id=task_id,
        data={"task_id": str(task_id) if task_id else None},
    )


def publish_log_updated(redis_url: str, *, task_id: str | uuid.UUID, storage_key: str) -> bool:
    return publish_ui_event(
        redis_url,
        topics=[f"task:{task_id}"],
        name="log.updated",
        entity_id=storage_key,
        data={"task_id": str(task_id), "storage_key": storage_key},
    )


def publish_account_updated(redis_url: str, account: Any) -> bool:
    account_id = str(getattr(account, "id", "") or "")
    if not account_id:
        return False
    data = {
        "id": account_id,
        "platform": _enum_value(getattr(account, "platform", "")),
        "name": getattr(account, "name", ""),
        "is_active": bool(getattr(account, "is_active", False)),
        "check_state": str(getattr(account, "check_state", "") or ""),
        "last_checked_at": getattr(account, "last_checked_at", None),
        "last_check_message": getattr(account, "last_check_message", None),
        "created_at": getattr(account, "created_at", None),
    }
    return publish_ui_event(
        redis_url,
        topics=["publishing"],
        name="publish_account.updated",
        entity_id=account_id,
        data=data,
    )


def publish_login_session_updated(redis_url: str, session: Any) -> bool:
    session_id = str(getattr(session, "id", "") or "")
    if not session_id:
        return False
    data = {
        "id": session_id,
        "platform": str(getattr(session, "platform", "") or ""),
        "account_name": str(getattr(session, "account_name", "") or ""),
        "state": str(getattr(session, "state", "") or ""),
        "message": getattr(session, "message", None),
        "browser_url": str(getattr(session, "browser_url", "") or ""),
        "created_at": getattr(session, "created_at", None),
        "finished_at": getattr(session, "finished_at", None),
    }
    return publish_ui_event(
        redis_url,
        topics=["publishing"],
        name="login_session.updated",
        entity_id=session_id,
        data=data,
    )


def publish_agent_event(
    redis_url: str,
    *,
    run_id: str | uuid.UUID,
    name: str,
    data: dict[str, Any],
) -> bool:
    return publish_ui_event(
        redis_url,
        topics=["agents"],
        name=name,
        entity_id=run_id,
        data=data,
    )


def _model_event_spec(obj: Any, *, deleted: bool = False) -> dict[str, Any] | None:
    from videoroll.db.models import Account, Asset, PublishBatch, PublishJob, RenderJob, SubtitleJob, Task

    if isinstance(obj, Task):
        task_id = str(obj.id or "")
        if not task_id:
            return None
        return {
            "topics": ["tasks", "queue", f"task:{task_id}"],
            "name": "task.deleted" if deleted else "task.updated",
            "entity_id": task_id,
            "data": {
                "id": task_id,
                "source_type": _enum_value(obj.source_type),
                "source_url": obj.source_url,
                "source_license": _enum_value(obj.source_license),
                "source_proof_url": obj.source_proof_url,
                "status": _enum_value(obj.status),
                "priority": int(obj.priority or 0),
                "created_by": obj.created_by,
                "error_code": obj.error_code,
                "error_message": obj.error_message,
                "retry_count": int(obj.retry_count or 0),
                "created_at": obj.created_at,
                "updated_at": obj.updated_at,
            },
        }
    if isinstance(obj, SubtitleJob):
        task_id = str(obj.task_id or "")
        return {
            "topics": ["tasks", "queue", f"task:{task_id}"],
            "name": "subtitle_job.deleted" if deleted else "subtitle_job.updated",
            "entity_id": str(obj.id),
            "data": {
                "id": str(obj.id),
                "task_id": task_id,
                "status": _enum_value(obj.status),
                "progress": int(obj.progress or 0),
                "error_message": obj.error_message,
                "created_at": obj.created_at,
                "updated_at": obj.updated_at,
            },
        }
    if isinstance(obj, RenderJob):
        task_id = str(obj.task_id or "")
        return {
            "topics": ["tasks", "queue", f"task:{task_id}"],
            "name": "render_job.deleted" if deleted else "render_job.updated",
            "entity_id": str(obj.id),
            "data": {
                "id": str(obj.id),
                "task_id": task_id,
                "subtitle_job_id": str(obj.subtitle_job_id) if obj.subtitle_job_id else None,
                "status": _enum_value(obj.status),
                "progress": int(obj.progress or 0),
                "error_message": obj.error_message,
                "created_at": obj.created_at,
                "updated_at": obj.updated_at,
            },
        }
    if isinstance(obj, PublishJob):
        task_id = str(obj.task_id or "")
        return {
            "topics": ["tasks", "publishing", f"task:{task_id}"],
            "name": "publish_job.deleted" if deleted else "publish_job.updated",
            "entity_id": str(obj.id),
            "data": {
                "id": str(obj.id),
                "task_id": task_id,
                "batch_id": str(obj.batch_id) if obj.batch_id else None,
                "platform": _enum_value(obj.platform),
                "state": _enum_value(obj.state),
                "aid": obj.aid,
                "bvid": obj.bvid,
                "external_id": obj.external_id,
                "external_url": obj.external_url,
                "account_id": str(obj.account_id) if obj.account_id else None,
                "upload_progress": int(getattr(obj, "upload_progress", 0) or 0),
                "upload_active": bool(getattr(obj, "upload_active", False)),
                "started_at": obj.started_at,
                "finished_at": obj.finished_at,
                "created_at": obj.created_at,
                "updated_at": obj.updated_at,
            },
        }
    if isinstance(obj, PublishBatch):
        task_id = str(obj.task_id or "")
        return {
            "topics": ["tasks", "publishing", f"task:{task_id}"],
            "name": "publish_batch.deleted" if deleted else "publish_batch.updated",
            "entity_id": str(obj.id),
            "data": {
                "id": str(obj.id),
                "task_id": task_id,
                "state": obj.state,
                "expected_targets": list(obj.expected_targets or []),
                "outcomes": dict(obj.outcomes_json or {}),
                "cleanup_enqueued_at": obj.cleanup_enqueued_at,
                "finished_at": obj.finished_at,
                "created_at": obj.created_at,
                "updated_at": obj.updated_at,
            },
        }
    if isinstance(obj, Asset):
        task_id = str(obj.task_id or "")
        return {
            "topics": ["tasks", f"task:{task_id}"],
            "name": "asset.deleted" if deleted else "asset.updated",
            "entity_id": str(obj.id),
            "data": {
                "id": str(obj.id),
                "task_id": task_id,
                "kind": _enum_value(obj.kind),
                "storage_key": obj.storage_key,
                "sha256": obj.sha256,
                "size_bytes": obj.size_bytes,
                "duration_ms": obj.duration_ms,
                "created_at": obj.created_at,
            },
        }
    if isinstance(obj, Account):
        return {
            "topics": ["publishing"],
            "name": "publish_account.deleted" if deleted else "publish_account.updated",
            "entity_id": str(obj.id),
            "data": {
                "id": str(obj.id),
                "platform": _enum_value(obj.platform),
                "name": obj.name,
                "is_active": bool(obj.is_active),
                "check_state": str(obj.check_state or ""),
                "last_checked_at": obj.last_checked_at,
                "last_check_message": obj.last_check_message,
                "created_at": obj.created_at,
            },
        }
    return None


def install_session_event_emitter() -> None:
    global _SESSION_EVENTS_INSTALLED
    if _SESSION_EVENTS_INSTALLED:
        return
    _SESSION_EVENTS_INSTALLED = True

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    @event.listens_for(Session, "after_flush")
    def collect_realtime_events(session: Session, _flush_context: Any) -> None:
        pending: dict[tuple[str, str], dict[str, Any]] = session.info.setdefault("realtime_events", {})
        for obj in list(session.new) + list(session.dirty):
            spec = _model_event_spec(obj)
            if spec:
                pending[(str(spec["name"]), str(spec["entity_id"]))] = spec
        for obj in session.deleted:
            spec = _model_event_spec(obj, deleted=True)
            if spec:
                pending[(str(spec["name"]), str(spec["entity_id"]))] = spec

    @event.listens_for(Session, "after_commit")
    def emit_realtime_events(session: Session) -> None:
        pending = session.info.pop("realtime_events", {})
        redis_url = str(os.getenv("REDIS_URL", "") or "").strip()
        if not redis_url:
            return
        for spec in pending.values():
            publish_ui_event(redis_url, **spec)

    @event.listens_for(Session, "after_rollback")
    def clear_realtime_events(session: Session) -> None:
        session.info.pop("realtime_events", None)
