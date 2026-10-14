"""Durable cooperative controls: no SIGKILL, no task-level cancellation."""
from __future__ import annotations
import uuid
from sqlalchemy.orm import Session
from videoroll.apps.dubbing.quality import RUNNING
from videoroll.db.models import DubbingJob

class DubbingInterrupted(RuntimeError):
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"dubbing job {status}")

def request_control(db: Session, task_id: uuid.UUID, job_id: uuid.UUID, *, action: str) -> DubbingJob:
    if action not in ("pause", "cancel"):
        raise ValueError("unsupported operation")
    job = db.query(DubbingJob).filter(DubbingJob.task_id == task_id, DubbingJob.id == job_id).with_for_update().one_or_none()
    if job is None:
        raise LookupError("dubbing job not found")
    if job.status in ("succeeded", "canceled"):
        raise ValueError("completed or canceled dubbing job is immutable")
    if job.status in ("paused", "preview_ready", "needs_review", "failed", "queued"):
        job.status = "paused" if action == "pause" else "canceled"
        job.lease_owner = None
        job.lease_until = None
    elif job.status in RUNNING or job.status in ("pause_requested", "cancel_requested"):
        job.status = "pause_requested" if action == "pause" else "cancel_requested"
    else:
        raise ValueError(f"cannot {action} dubbing job in status {job.status}")
    db.commit()
    return job

def check_control(db: Session, job_id: uuid.UUID, owner: str) -> None:
    row = db.get(DubbingJob, job_id)
    if row is None or row.lease_owner != owner:
        raise DubbingInterrupted("lease_lost")
    if row.status == "cancel_requested":
        raise DubbingInterrupted("canceled")
    if row.status == "pause_requested":
        raise DubbingInterrupted("paused")
    if row.status in ("paused", "canceled"):
        raise DubbingInterrupted(row.status)
