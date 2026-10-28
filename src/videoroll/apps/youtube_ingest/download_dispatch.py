"""Durable YouTube downloader supervised independently of the Orchestrator API.

The database operation inbox is the source of truth. A worker process restart
can reclaim expired leases and reuse on-disk aria2 checkpoints. New downloads
are gated by the number saved on the Processing Queue page, not by Hatchet
workflow fan-out or a hardcoded pool limit.
"""
from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.config import get_orchestrator_settings
from videoroll.db.models import AppSetting, OperationInbox, PipelineRun, Task, TaskStatus, SourceType
from videoroll.db.session import get_sessionmaker
from videoroll.storage.filesystem import FileStore

logger = logging.getLogger(__name__)
SETTING_KEY = "workflow.youtube_download_concurrency"
DEFAULT_CONCURRENCY = 2
MAX_CONCURRENCY = 16
OPERATION_PREFIX = "youtube-download:"


def read_download_concurrency(db: Session) -> int:
    row = db.get(AppSetting, SETTING_KEY)
    try:
        value = int((row.value_json or {}).get("max_concurrent", DEFAULT_CONCURRENCY)) if row else DEFAULT_CONCURRENCY
    except (ValueError, TypeError, AttributeError):
        value = DEFAULT_CONCURRENCY
    return max(1, min(MAX_CONCURRENCY, value))


def set_download_concurrency(db: Session, value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CONCURRENCY:
        raise ValueError(f"simultaneous downloads must be between 1 and {MAX_CONCURRENCY}")
    row = db.get(AppSetting, SETTING_KEY)
    if row is None:
        row = AppSetting(key=SETTING_KEY, value_json={})
    row.value_json = {"max_concurrent": value}
    db.add(row)
    db.commit()
    return value


def enqueue_download(db: Session, task_id: uuid.UUID) -> str:
    """Durable, idempotent handoff. Never run the download in an HTTP worker."""
    task = db.get(Task, task_id)
    if task is None or task.source_type != SourceType.youtube:
        raise ValueError("YouTube task not found")
    explicit_restart = str(task.created_by or "").startswith("youtube_task_restart;")
    if task.status in {TaskStatus.published, TaskStatus.publishing, TaskStatus.canceled} or (task.status == TaskStatus.failed and not explicit_restart):
        raise ValueError("task is terminal and cannot enqueue an automatic download")
    key = f"{OPERATION_PREFIX}{task_id}"
    operation = db.query(OperationInbox).filter(OperationInbox.operation_key == key).with_for_update().one_or_none()
    if operation is None:
        try:
            with db.begin_nested():
                db.add(OperationInbox(operation_key=key, status="pending", request_json={"task_id": str(task_id)}))
                db.flush()
        except IntegrityError:
            # Concurrent idempotent enqueues use the winner's durable record.
            operation = db.query(OperationInbox).filter(OperationInbox.operation_key == key).with_for_update().one()
    if operation is not None and operation.status == "completed":
        return "completed"
    if operation is not None and operation.status == "failed":
        # Explicit UI restart supplies a new durable run marker and may reopen
        # the same task; an automatic retry cannot erase a terminal failure.
        if not explicit_restart or (operation.request_json or {}).get("explicit_restart_id") == task.created_by:
            return "failed"
        operation.status = "pending"
        operation.lease_owner = None
        operation.lease_until = None
        operation.last_error = None
        operation.request_json = {"task_id": str(task_id), "explicit_restart_id": task.created_by}

    db.commit()
    return "queued"


def download_operation_status(db: Session, task_id: uuid.UUID) -> tuple[str, datetime | None, str | None] | None:
    row = db.query(OperationInbox).filter(OperationInbox.operation_key == f"{OPERATION_PREFIX}{task_id}").one_or_none()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if row.status == "processing" and (row.lease_until is None or row.lease_until <= now):
        return ("queued", row.updated_at, "Download owner lost; awaiting lease recovery")
    return (row.status, row.updated_at, row.last_error)


class DownloadSupervisor:
    """One supervisor belongs to the separate youtube-ingest service process."""

    def __init__(self, *, poll_seconds: float = 3.0):
        self.settings = get_orchestrator_settings()
        self.sessions = get_sessionmaker(self.settings.database_url)
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY, thread_name_prefix="youtube-download")
        self.inflight: dict[uuid.UUID, object] = {}

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="youtube-download-supervisor")
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=6)
        # The process owns active requests; systemd restart can terminate them.
        # Their leases and .aria2 checkpoints survive for a later reclaim.
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _execute(self, task_id: uuid.UUID) -> None:
        from videoroll.apps.orchestrator_api.services.youtube_service import download

        with self.sessions() as db:
            try:
                download(task_id, settings=self.settings, db=db, store=FileStore(self.settings))
                logger.info("youtube download completed task=%s", task_id)
            except Exception as exc:
                db.rollback()
                logger.exception("youtube download attempt failed task=%s", task_id)
                key = f"{OPERATION_PREFIX}{task_id}"
                with self.sessions() as record:
                    op = record.query(OperationInbox).filter(OperationInbox.operation_key == key).with_for_update().one_or_none()
                    if op is None or op.status == "completed":
                        return
                    request = dict(op.request_json or {})
                    attempts = int(request.get("worker_failures", 0)) + 1
                    request["worker_failures"] = attempts
                    request["retry_after"] = (datetime.now(timezone.utc) + timedelta(seconds=min(900, 30 * (2 ** min(attempts, 5))))).isoformat()
                    op.request_json = request
                    op.last_error = f"{type(exc).__name__}: {exc}"[:900]
                    if attempts >= 4:
                        op.status = "failed"
                        op.lease_owner = None
                        op.lease_until = None
                        task = record.get(Task, task_id)
                        if task is not None and task.status == TaskStatus.ingested:
                            task.status = TaskStatus.failed
                            task.error_code = "YOUTUBE_DOWNLOAD_FAILED"
                            task.error_message = op.last_error
                            record.add(task)
                    record.commit()

    def _candidates(self, db: Session, *, limit: int) -> list[uuid.UUID]:
        now = datetime.now(timezone.utc)
        operations = (db.query(OperationInbox)
            .filter(OperationInbox.operation_key.like(f"{OPERATION_PREFIX}%"),
                    OperationInbox.status.in_(("pending", "processing")),
                    or_(OperationInbox.status == "pending", OperationInbox.lease_until < now))
            .order_by(OperationInbox.created_at.asc()).limit(500).all())
        keys = []
        for op in operations:
            try:
                keys.append((uuid.UUID(op.operation_key[len(OPERATION_PREFIX):]), op))
            except (ValueError, AttributeError):
                continue
        tasks = db.query(Task).filter(Task.id.in_([task_id for task_id, _ in keys]),
                                      Task.status.in_((TaskStatus.ingested, TaskStatus.failed)),
                                      Task.source_type == SourceType.youtube).all() if keys else []
        available = {task.id: task for task in tasks if task.status == TaskStatus.ingested or
                     str(task.created_by or "").startswith("youtube_task_restart;")}
        ordered = sorted(((task_id, op) for task_id, op in keys if task_id in available),
                         key=lambda pair: (-int(available[pair[0]].priority or 0), pair[1].created_at))
        found = []
        for task_id, op in ordered:
            if task_id in self.inflight:
                continue
            # Do not revive a task whose parent workflow has already failed,
            # been cancelled, or was superseded by a manual restart.
            parent = (db.query(PipelineRun).filter(
                PipelineRun.task_id == task_id,
                PipelineRun.workflow_name == "VideoPipelineV1",
            ).order_by(PipelineRun.created_at.desc()).first())
            if parent is None or parent.state not in {"submitted", "running"}:
                continue
            req = op.request_json if isinstance(op.request_json, dict) else {}
            after = req.get("retry_after")
            if after:
                try:
                    if datetime.fromisoformat(after) > now:
                        continue
                except (TypeError, ValueError):
                    pass
            found.append(task_id)
            if len(found) >= limit:
                break
        return found

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.inflight = {key: future for key, future in self.inflight.items() if not future.done()}
                with self.sessions() as db:
                    target = read_download_concurrency(db)
                    now = datetime.now(timezone.utc)
                    # Include live operations owned by other processes, such as
                    # an in-flight legacy HTTP download during migration.
                    all_live = db.query(OperationInbox).filter(
                        OperationInbox.operation_key.like(f"{OPERATION_PREFIX}%"),
                        OperationInbox.status == "processing",
                        OperationInbox.lease_until > now,
                    ).count()
                    available = max(0, target - max(len(self.inflight), all_live))
                    candidates = self._candidates(db, limit=available) if available else []
                    db.rollback()
                for task_id in candidates:
                    self.inflight[task_id] = self.pool.submit(self._execute, task_id)
            except Exception:
                logger.exception("download supervisor scan failed")
            self.stop_event.wait(self.poll_seconds)
