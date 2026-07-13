from __future__ import annotations

import os
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from celery.app.control import flatten_reply
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.render_queue_store import get_task_queue_settings
from videoroll.db.models import (
    PublishJob,
    PublishState,
    RenderJob,
    RenderJobStatus,
    SubtitleJob,
    SubtitleJobStatus,
)
from videoroll.db.session import get_sessionmaker

_MIN_WORKER_CONCURRENCY = 1
_MAX_WORKER_CONCURRENCY = 32
_RUNTIME_CONTROL_TIMEOUT_SECONDS = 1.5
_SUBTITLE_QUEUE_NAME = "subtitle"
_MIN_JOB_LEASE_SECONDS = 1
_MAX_JOB_LEASE_SECONDS = 3600


@dataclass(frozen=True)
class RecoverySummary:
    """Counts rows safely returned to a worker queue after lease expiry."""

    subtitle_requeued: int = 0
    render_requeued: int = 0

    @property
    def total_recovered(self) -> int:
        return self.subtitle_requeued + self.render_requeued


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_seconds(value: int) -> int:
    return max(_MIN_JOB_LEASE_SECONDS, min(int(value or _MIN_JOB_LEASE_SECONDS), _MAX_JOB_LEASE_SECONDS))


def _lease_owner(value: object) -> str:
    owner = str(value or "").strip()[:128]
    if not owner:
        raise ValueError("lease owner is required")
    return owner


def _job_model_and_state(job: object) -> tuple[type[SubtitleJob] | type[RenderJob] | type[PublishJob], object]:
    model = type(job)
    if model not in {SubtitleJob, RenderJob, PublishJob}:
        raise TypeError(f"unsupported lease job type: {model!r}")
    if hasattr(model, "status"):
        return model, getattr(job, "status")
    return model, getattr(job, "state")


def acquire_job_lease(db: Session, job: object, owner: str, ttl_seconds: int) -> bool:
    """Atomically claim an unleased or expired job without trusting Redis locks."""
    model, state = _job_model_and_state(job)
    owner = _lease_owner(owner)
    now = _utcnow()
    state_column = model.status if hasattr(model, "status") else model.state
    result = db.execute(
        update(model)
        .where(
            model.id == getattr(job, "id"),
            state_column == state,
            or_(model.lease_until.is_(None), model.lease_until <= now),
        )
        .values(
            lease_owner=owner,
            lease_until=now + timedelta(seconds=_lease_seconds(ttl_seconds)),
            heartbeat_at=now,
        )
    )
    if not bool(result.rowcount):
        return False
    db.expire(job)
    db.flush()
    return True


def heartbeat_job_lease(db: Session, job_id: uuid.UUID | str, owner: str, ttl_seconds: int) -> bool:
    """Extend only a still-live lease held by ``owner``.

    Job UUIDs are generated independently per table, so the update deliberately
    checks each lease-bearing job table.  The owner and current active state are
    both conditions, preventing an old worker from reviving an expired or
    completed job.
    """
    owner = _lease_owner(owner)
    now = _utcnow()
    values = {
        "lease_until": now + timedelta(seconds=_lease_seconds(ttl_seconds)),
        "heartbeat_at": now,
    }
    active_models = (
        (SubtitleJob, (SubtitleJobStatus.running,)),
        (RenderJob, (RenderJobStatus.running,)),
        # Publish workers obtain the lease before downloading assets, while the
        # row is still draft; submitting begins immediately before the external
        # platform side effect.
        (PublishJob, (PublishState.draft, PublishState.submitting)),
    )
    updated = 0
    for model, active_states in active_models:
        result = db.execute(
            update(model)
            .where(
                model.id == job_id,
                (model.status if hasattr(model, "status") else model.state).in_(active_states),
                model.lease_owner == owner,
                model.lease_until.is_not(None),
                model.lease_until > now,
            )
            .values(**values)
        )
        updated += int(result.rowcount or 0)
    if not updated:
        return False
    db.flush()
    return True


def release_job_lease(db: Session, job_id: uuid.UUID | str, owner: str) -> bool:
    """Release a lease only when the completing worker still owns it."""
    owner = _lease_owner(owner)
    now = _utcnow()
    updated = 0
    for model in (SubtitleJob, RenderJob, PublishJob):
        result = db.execute(
            update(model)
            .where(model.id == job_id, model.lease_owner == owner)
            .values(lease_owner=None, lease_until=None, heartbeat_at=now)
        )
        updated += int(result.rowcount or 0)
    if not updated:
        return False
    db.flush()
    return True


def _recovery_message(message: str | None, detail: str) -> str:
    combined = f"{str(message or '').strip()}\n{detail}".strip()
    return combined[-2000:]


def recover_expired_leases(db: Session, now: datetime, limit: int) -> RecoverySummary:
    """Requeue interrupted subtitle/render work whose owner lease expired.

    Rows with no lease are intentionally ignored.  They predate lease-based
    execution or are managed by another workflow; treating them as dead would
    recreate the old global-running-row recovery bug.
    """
    remaining = max(0, int(limit or 0))
    if not remaining:
        return RecoverySummary()

    subtitle_requeued = 0
    render_requeued = 0
    subtitle_jobs = (
        db.query(SubtitleJob)
        .filter(
            SubtitleJob.status == SubtitleJobStatus.running,
            SubtitleJob.lease_until.is_not(None),
            SubtitleJob.lease_until <= now,
        )
        .order_by(SubtitleJob.lease_until.asc(), SubtitleJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(remaining)
        .all()
    )
    for job in subtitle_jobs:
        request = dict(job.request_json) if isinstance(job.request_json, dict) else {}
        request["resume"] = True
        job.request_json = request
        job.status = SubtitleJobStatus.queued
        job.progress = 0
        job.lease_owner = None
        job.lease_until = None
        job.heartbeat_at = now
        job.error_message = _recovery_message(
            job.error_message,
            "Worker lease expired while subtitle processing; requeued with resume enabled.",
        )
        db.add(job)
        subtitle_requeued += 1

    remaining -= subtitle_requeued
    if remaining:
        render_jobs = (
            db.query(RenderJob)
            .filter(
                RenderJob.status == RenderJobStatus.running,
                RenderJob.lease_until.is_not(None),
                RenderJob.lease_until <= now,
            )
            .order_by(RenderJob.lease_until.asc(), RenderJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(remaining)
            .all()
        )
        for job in render_jobs:
            job.status = RenderJobStatus.queued
            job.progress = 0
            job.retry_count = int(job.retry_count or 0) + 1
            job.started_at = None
            job.finished_at = None
            job.lease_owner = None
            job.lease_until = None
            job.heartbeat_at = now
            job.error_message = _recovery_message(
                job.error_message,
                "Worker lease expired while rendering; requeued for resume.",
            )
            db.add(job)
            render_requeued += 1

    db.flush()
    return RecoverySummary(subtitle_requeued=subtitle_requeued, render_requeued=render_requeued)


class JobLeaseHeartbeat:
    """Refresh a job lease from a dedicated short-lived database session."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        job_id: uuid.UUID | str,
        owner: str,
        ttl_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._owner = _lease_owner(owner)
        self._ttl_seconds = _lease_seconds(ttl_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"job-lease-heartbeat-{self._job_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        interval = max(1.0, self._ttl_seconds / 3)
        while not self._stop.wait(interval):
            db = self._session_factory()
            try:
                if heartbeat_job_lease(db, self._job_id, self._owner, self._ttl_seconds):
                    db.commit()
                else:
                    db.rollback()
                    return
            except Exception:
                db.rollback()
            finally:
                db.close()


def normalize_subtitle_worker_concurrency(value: Any, *, fallback: int = 1) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(fallback)
    if n < _MIN_WORKER_CONCURRENCY:
        return _MIN_WORKER_CONCURRENCY
    if n > _MAX_WORKER_CONCURRENCY:
        return _MAX_WORKER_CONCURRENCY
    return n


def subtitle_worker_concurrency_for_task_queue_settings(settings: dict[str, Any], *, fallback: int = 1) -> int:
    if not isinstance(settings, dict):
        return normalize_subtitle_worker_concurrency(fallback, fallback=fallback)
    return normalize_subtitle_worker_concurrency(settings.get("max_concurrency"), fallback=fallback)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def subtitle_worker_destinations(
    celery_app: Any,
    *,
    queue: str = _SUBTITLE_QUEUE_NAME,
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> list[str]:
    inspector = celery_app.control.inspect(timeout=timeout)
    active_queues = inspector.active_queues() or {}

    hosts: list[str] = []
    for hostname, queue_infos in _as_dict(active_queues).items():
        host = str(hostname or "").strip()
        if not host:
            continue
        for queue_info in _as_list(queue_infos):
            if str(_as_dict(queue_info).get("name") or "").strip() == queue:
                hosts.append(host)
                break
    return sorted(set(hosts))


def _extract_worker_pool_concurrency(stats: dict[str, Any]) -> int | None:
    pool = _as_dict(stats.get("pool"))
    raw: Any = pool.get("max-concurrency")
    if raw is None:
        raw = pool.get("max_concurrency")
    if raw is None:
        processes = pool.get("processes")
        if isinstance(processes, list) and processes:
            raw = len(processes)
    if raw is None:
        return None
    return normalize_subtitle_worker_concurrency(raw)


def subtitle_worker_pool_concurrency(
    celery_app: Any,
    *,
    destinations: Sequence[str],
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, int]:
    if not destinations:
        return {}

    inspector = celery_app.control.inspect(destination=list(destinations), timeout=timeout)
    stats = inspector.stats() or {}

    resolved: dict[str, int] = {}
    for hostname, info in _as_dict(stats).items():
        host = str(hostname or "").strip()
        if not host:
            continue
        pool_concurrency = _extract_worker_pool_concurrency(_as_dict(info))
        if pool_concurrency is None:
            continue
        resolved[host] = pool_concurrency
    return resolved


def _flatten_control_reply(reply: Any) -> dict[str, Any]:
    if isinstance(reply, list):
        return flatten_reply(reply)
    return _as_dict(reply)


def _control_reply_status(reply: Any) -> tuple[bool, str | None]:
    data = _as_dict(reply)
    ok = data.get("ok")
    if ok not in (None, False, ""):
        return True, str(ok).strip() or "ok"

    error = data.get("error")
    if error not in (None, ""):
        return False, str(error).strip()

    nok = data.get("nok")
    if nok not in (None, ""):
        return False, str(nok).strip()

    if isinstance(reply, str) and reply.strip():
        return True, reply.strip()

    return False, None


def sync_subtitle_worker_concurrency(
    celery_app: Any,
    target_concurrency: Any,
    *,
    queue: str = _SUBTITLE_QUEUE_NAME,
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    target = normalize_subtitle_worker_concurrency(target_concurrency)
    result: dict[str, Any] = {
        "ok": False,
        "queue": queue,
        "target_concurrency": target,
        "detail": None,
        "workers": [],
    }

    try:
        destinations = subtitle_worker_destinations(celery_app, queue=queue, timeout=timeout)
    except Exception as e:
        result["detail"] = f"failed to inspect workers for queue '{queue}': {type(e).__name__}: {e}"
        return result

    if not destinations:
        result["detail"] = f"no running workers consume queue '{queue}'"
        return result

    try:
        current_by_host = subtitle_worker_pool_concurrency(celery_app, destinations=destinations, timeout=timeout)
    except Exception as e:
        result["detail"] = f"failed to inspect worker pool stats: {type(e).__name__}: {e}"
        return result

    workers: list[dict[str, Any]] = []
    failed_hosts: list[str] = []
    changed_hosts = 0

    for hostname in destinations:
        current = current_by_host.get(hostname)
        item: dict[str, Any] = {
            "hostname": hostname,
            "current_concurrency": current,
            "target_concurrency": target,
            "action": "noop",
            "ok": False,
            "detail": None,
        }
        if current is None:
            item["detail"] = "worker stats unavailable"
            failed_hosts.append(hostname)
            workers.append(item)
            continue

        delta = target - current
        if delta == 0:
            item["ok"] = True
            item["detail"] = "already at target"
            workers.append(item)
            continue

        try:
            if delta > 0:
                item["action"] = "grow"
                reply = celery_app.control.pool_grow(delta, destination=[hostname], reply=True, timeout=timeout)
            else:
                item["action"] = "shrink"
                reply = celery_app.control.pool_shrink(-delta, destination=[hostname], reply=True, timeout=timeout)
        except Exception as e:
            item["detail"] = f"{type(e).__name__}: {e}"
            failed_hosts.append(hostname)
            workers.append(item)
            continue

        reply_by_host = _flatten_control_reply(reply)
        ok, detail = _control_reply_status(reply_by_host.get(hostname))
        item["ok"] = ok
        if ok:
            changed_hosts += 1
            item["detail"] = detail or f"pool will {item['action']}"
        else:
            item["detail"] = detail or f"worker did not acknowledge pool_{item['action']}"
            failed_hosts.append(hostname)
        workers.append(item)

    result["workers"] = workers
    result["ok"] = not failed_hosts and bool(workers)
    if failed_hosts:
        result["detail"] = f"sync incomplete for {len(failed_hosts)}/{len(workers)} worker(s): {', '.join(failed_hosts)}"
    elif changed_hosts > 0:
        result["detail"] = f"synchronized {len(workers)} worker(s) to concurrency={target}"
    else:
        result["detail"] = f"worker concurrency already at {target}"
    return result


def sync_subtitle_worker_concurrency_for_task_queue_settings(
    celery_app: Any,
    settings: dict[str, Any],
    *,
    queue: str = _SUBTITLE_QUEUE_NAME,
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    target = subtitle_worker_concurrency_for_task_queue_settings(settings)
    return sync_subtitle_worker_concurrency(celery_app, target, queue=queue, timeout=timeout)


def resolve_subtitle_worker_concurrency(database_url: str, *, fallback: int = 1) -> int:
    db_url = str(database_url or "").strip()
    if not db_url:
        return normalize_subtitle_worker_concurrency(fallback, fallback=fallback)

    SessionLocal = get_sessionmaker(db_url)
    db = SessionLocal()
    try:
        cfg = get_task_queue_settings(db)
        return subtitle_worker_concurrency_for_task_queue_settings(cfg, fallback=fallback)
    finally:
        db.close()


def main() -> int:
    fallback = normalize_subtitle_worker_concurrency(os.getenv("CELERY_SUB_CONCURRENCY_FALLBACK", "1"))
    try:
        resolved = resolve_subtitle_worker_concurrency(os.getenv("DATABASE_URL", ""), fallback=fallback)
    except Exception as e:
        print(f"warning: failed to resolve subtitle worker concurrency from task queue settings: {e}", file=sys.stderr)
        resolved = fallback
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
