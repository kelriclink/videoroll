from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import PublishBatch, PublishJob, PublishState, Task, TaskStatus


class PublishBatchState(str, Enum):
    active = "active"
    succeeded = "succeeded"
    partial_failed = "partial_failed"
    failed = "failed"


@dataclass(frozen=True)
class PublishBatchEvaluation:
    batch_state: PublishBatchState
    task_status: TaskStatus
    cleanup_ready: bool


@dataclass(frozen=True)
class PublishBatchReconciliation:
    batch_id: uuid.UUID
    batch_state: PublishBatchState
    task_status: TaskStatus
    cleanup_enqueued: bool


_SUCCESS_STATES = {PublishState.submitted, PublishState.published}
_FAILURE_STATES = {PublishState.failed, PublishState.unknown}


def _value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def publish_target_key(platform: object, account_id: uuid.UUID | str | None) -> str:
    account = str(account_id) if account_id else "default"
    return f"{_value(platform)}:{account}"


def _state(value: object) -> PublishState | None:
    if isinstance(value, PublishState):
        return value
    try:
        return PublishState(_value(value))
    except ValueError:
        return None


def evaluate_publish_batch(
    expected_targets: list[dict[str, Any]],
    job_states: dict[str, PublishState | str],
    outcomes: dict[str, dict[str, Any]],
) -> PublishBatchEvaluation:
    """Derive the task state from this batch's immutable target snapshot."""
    has_success = False
    has_failure = False
    has_pending = False

    for target in expected_targets:
        key = str(target.get("key") or publish_target_key(target.get("platform"), target.get("account_id")))
        state = _state(job_states.get(key))
        if state is None:
            state = _state((outcomes.get(key) or {}).get("state"))
        if state in _SUCCESS_STATES:
            has_success = True
        elif state in _FAILURE_STATES:
            has_failure = True
        else:
            has_pending = True

    if expected_targets and not has_pending and not has_failure:
        return PublishBatchEvaluation(PublishBatchState.succeeded, TaskStatus.published, True)
    if has_pending:
        return PublishBatchEvaluation(PublishBatchState.active, TaskStatus.publishing, False)
    if has_success:
        return PublishBatchEvaluation(PublishBatchState.partial_failed, TaskStatus.publishing, False)
    return PublishBatchEvaluation(PublishBatchState.failed, TaskStatus.failed, False)


def reconcile_publish_batch(db: Session, batch_id: uuid.UUID) -> PublishBatchReconciliation:
    """Persist the aggregate state after one publisher updates a PublishJob."""
    batch = db.get(PublishBatch, batch_id, with_for_update=True)
    if not batch:
        raise ValueError("publish batch not found")

    latest_states: dict[str, PublishState] = {}
    jobs = (
        db.query(PublishJob)
        .filter(PublishJob.batch_id == batch.id)
        .order_by(PublishJob.updated_at.desc(), PublishJob.created_at.desc())
        .all()
    )
    for job in jobs:
        key = publish_target_key(job.platform, job.account_id)
        latest_states.setdefault(key, job.state)

    evaluation = evaluate_publish_batch(
        list(batch.expected_targets or []), latest_states, dict(batch.outcomes_json or {}),
    )
    batch.state = evaluation.batch_state.value
    if evaluation.batch_state != PublishBatchState.active:
        batch.finished_at = datetime.now(timezone.utc)

    cleanup_enqueued = False
    if evaluation.cleanup_ready and batch.cleanup_enqueued_at is None:
        batch.cleanup_enqueued_at = datetime.now(timezone.utc)
        cleanup_enqueued = True
    db.add(batch)

    task = db.get(Task, batch.task_id)
    if task and task.status != TaskStatus.canceled:
        task.status = evaluation.task_status
        if evaluation.batch_state == PublishBatchState.succeeded:
            task.error_code = None
            task.error_message = None
        elif evaluation.batch_state == PublishBatchState.partial_failed:
            task.error_code = "PUBLISH_PARTIAL_FAILURE"
            task.error_message = "one or more publish targets failed; retry only failed targets"
        elif evaluation.batch_state == PublishBatchState.failed:
            task.error_code = "PUBLISH_FAILED"
            task.error_message = "all publish targets failed"
        db.add(task)

    return PublishBatchReconciliation(
        batch_id=batch.id,
        batch_state=evaluation.batch_state,
        task_status=evaluation.task_status,
        cleanup_enqueued=cleanup_enqueued,
    )


def record_publish_batch_dispatch_error(
    db: Session,
    batch_id: uuid.UUID,
    *,
    platform: object,
    account_id: uuid.UUID | str | None,
    detail: str,
) -> PublishBatchReconciliation:
    batch = db.get(PublishBatch, batch_id, with_for_update=True)
    if not batch:
        raise ValueError("publish batch not found")
    outcomes = dict(batch.outcomes_json or {})
    outcomes[publish_target_key(platform, account_id)] = {"state": PublishState.failed.value, "detail": str(detail)}
    batch.outcomes_json = outcomes
    db.add(batch)
    return reconcile_publish_batch(db, batch_id)


def enqueue_publish_batch_cleanup(task_id: uuid.UUID, batch_id: uuid.UUID, *, needed: bool) -> None:
    """Queue cleanup only after the batch reconciler has claimed it once."""
    if not needed:
        return
    from celery import current_app

    current_app.send_task(
        "subtitle_service.cleanup_task",
        args=[str(task_id), str(batch_id)],
        queue="subtitle",
    )
