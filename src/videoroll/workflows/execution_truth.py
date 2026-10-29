from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from videoroll.db.models import PipelineRun, RenderJobStatus, SubtitleJobStatus


ACTIVE_PIPELINE_STATES = frozenset(("submitting", "submitted", "running", "cancel_requested"))


def active_workflow_job_ids(
    db: Session,
    *,
    workflow_name: str,
    request_field: str,
    task_ids: Iterable[uuid.UUID] | None = None,
    limit: int = 5000,
) -> set[uuid.UUID]:
    """Resolve durable job ownership from active PipelineRun request metadata."""
    query = db.query(PipelineRun).filter(
        PipelineRun.workflow_name == workflow_name,
        PipelineRun.state.in_(tuple(ACTIVE_PIPELINE_STATES)),
    )
    normalized_task_ids = list(task_ids or [])
    if normalized_task_ids:
        query = query.filter(PipelineRun.task_id.in_(normalized_task_ids))
    rows = query.order_by(PipelineRun.created_at.desc()).limit(max(1, min(int(limit), 10000))).all()

    job_ids: set[uuid.UUID] = set()
    for run in rows:
        raw_job_id = str((run.request_json or {}).get(request_field) or "").strip()
        if not raw_job_id:
            continue
        try:
            job_ids.add(uuid.UUID(raw_job_id))
        except ValueError:
            continue
    return job_ids


def active_subtitle_job_ids(
    db: Session,
    *,
    task_ids: Iterable[uuid.UUID] | None = None,
    limit: int = 5000,
) -> set[uuid.UUID]:
    return active_workflow_job_ids(
        db,
        workflow_name="SubtitleJobV1",
        request_field="job_id",
        task_ids=task_ids,
        limit=limit,
    )


def has_live_job_lease(job: Any, *, now: datetime | None = None) -> bool:
    lease_until = getattr(job, "lease_until", None)
    lease_owner = str(getattr(job, "lease_owner", None) or "").strip()
    if not lease_owner or lease_until is None:
        return False
    current = now or datetime.now(timezone.utc)
    lease_until = lease_until.replace(tzinfo=lease_until.tzinfo or timezone.utc)
    return lease_until > current


def subtitle_job_is_live(
    job: Any,
    active_job_ids: set[uuid.UUID],
    *,
    now: datetime | None = None,
) -> bool:
    if getattr(job, "id", None) in active_job_ids:
        return True
    return (
        getattr(job, "status", None) == SubtitleJobStatus.running
        and has_live_job_lease(job, now=now)
    )


def render_job_is_live(job: Any, *, now: datetime | None = None) -> bool:
    status = getattr(job, "status", None)
    if status == RenderJobStatus.queued:
        return True
    return status == RenderJobStatus.running and has_live_job_lease(job, now=now)
