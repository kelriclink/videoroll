"""Durable cooperative controls: no SIGKILL, no task-level cancellation."""
from __future__ import annotations
import uuid
from sqlalchemy.orm import Session
RUNNING = frozenset(('preparing', 'synthesizing', 'aligning', 'mixing'))
from videoroll.db.models import DubbingJob, RenderJob, RenderJobStatus

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
    if job.status == "rendering":
        from videoroll.apps.outbox.service import create_outbox_event
        from videoroll.apps.subtitle_service.queues import SUBTITLE_CONTROL_QUEUE
        render_id = str((job.output_json or {}).get("render_job_id") or "")
        if not render_id:
            raise ValueError("rendering dubbing job has no RenderJob reference")
        render = db.query(RenderJob).filter(RenderJob.id == uuid.UUID(render_id)).with_for_update().one_or_none()
        if render is None or render.task_id != task_id:
            raise ValueError("render job disappeared")
        if render.status == RenderJobStatus.succeeded:
            raise ValueError("P2 video is already finished; refresh the status")
        if render.status in {RenderJobStatus.queued, RenderJobStatus.running}:
            # The active worker sees the canceled render status on its next
            # cancellation poll. Preserve execution lease/fence until it exits.
            render.status = RenderJobStatus.canceled
            render.error_message = f"P2 render {action} requested"
            db.add(render)
        job.status = "paused" if action == "pause" else "canceled"
        job.lease_owner = job.lease_until = None
        create_outbox_event(db, event_type="workflow.dubbing.finished",
            aggregate_type="dubbing_job", aggregate_id=job.id,
            task_name="subtitle_service.push_dubbing_workflow_event",
            args={"args": [str(job.id), "failed", f"P2 render {action} requested"],
                  "queue": SUBTITLE_CONTROL_QUEUE},
            operation_key=f"workflow-dubbing-finished:{job.id}:failed")
    elif job.status in ("paused", "preview_ready", "needs_review", "failed", "queued"):
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
