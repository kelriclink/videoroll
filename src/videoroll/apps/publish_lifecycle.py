from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy import String, cast, exists
from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.queues import SUBTITLE_CONTROL_QUEUE
from videoroll.db.models import (
    OperationInbox,
    OutboxEvent,
    Platform,
    PublishBatch,
    PublishJob,
    PublishState,
    Task,
    TaskStatus,
)

PUBLISH_CLEANUP_RETENTION = timedelta(hours=24)
# Broker acceptance is durable. A queued job must not be re-enqueued every
# recovery tick merely because a single-concurrency publisher has not reached it.


def publish_cleanup_due_at(batch: PublishBatch, current_batches: list[PublishBatch] | None = None) -> datetime:
    """Wait 24h after the last completed target, not the first channel."""
    finished_times = [row.finished_at for row in (current_batches or [batch]) if isinstance(row.finished_at, datetime)]
    finished = max((value.replace(tzinfo=value.tzinfo or timezone.utc) for value in finished_times),
                   default=datetime.now(timezone.utc))
    return finished + PUBLISH_CLEANUP_RETENTION



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
    cleanup_needed: bool


@dataclass(frozen=True)
class PublishDispatchRecovery:
    """Rows made safe to retry after a broker/worker interruption."""

    requeued: int = 0
    unknown: int = 0


_SUCCESS_STATES = {PublishState.submitted, PublishState.published}
_FAILURE_STATES = {PublishState.failed, PublishState.unknown}


def _value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def publish_target_key(platform: object, account_id: uuid.UUID | str | None) -> str:
    account = str(account_id) if account_id else "default"
    return f"{_value(platform)}:{account}"


def publish_operation_key(job_id: uuid.UUID | str) -> str:
    """Stable side-effect key for every delivery attempt of one publish job."""
    return f"publish-job:{job_id}"


def enqueue_publish_job_dispatch(db: Session, job: PublishJob, *, redeliver: bool = False) -> None:
    """Create the publisher outbox row in the transaction that creates ``job``.

    The HTTP services deliberately do not call Celery directly.  Once this
    returns, committing the caller's transaction makes both the desired
    publish state and its delivery intent durable together.  The shared
    outbox dispatcher owns all broker retries.
    """
    if job.id is None:
        db.flush()
    operation_key = str(job.operation_key or publish_operation_key(job.id))
    job.operation_key = operation_key
    platform = _value(job.platform)
    if platform == Platform.bilibili.value:
        task_name = "bilibili_publisher.process_job"
        queue = "publish"
    elif platform in {Platform.douyin.value, Platform.xiaohongshu.value, Platform.kuaishou.value}:
        task_name = "social_publisher.process_job"
        queue = "social_publish"
    else:
        raise ValueError(f"unsupported publish dispatch platform: {platform}")

    from videoroll.apps.outbox.service import create_outbox_event

    create_outbox_event(
        db,
        event_type="publish.dispatch",
        aggregate_type="publish_job",
        aggregate_id=job.id,
        task_name=task_name,
        args={"args": [str(job.id)], "queue": queue},
        operation_key=operation_key,
    )
    if redeliver:
        from videoroll.apps.outbox.service import redeliver_dispatched_event

        redeliver_dispatched_event(db, operation_key)
    db.add(job)


def recover_stale_publish_dispatches(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> PublishDispatchRecovery:
    """Recover lost publisher deliveries without flooding a backed-up queue.

    An unleased job may still have a Redis message or an in-flight delivery.
    Only jobs missing a durable outbox operation are automatically repaired.
    External-start markers or expired leases are classified as unknown.
    """
    now = now or datetime.now(timezone.utc)
    rows = (
        db.query(PublishJob)
        .filter(PublishJob.state == PublishState.submitting)
        .order_by(PublishJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(max(1, min(int(limit or 1), 500)))
        .all()
    )
    requeued = 0
    unknown = 0
    for job in rows:
        # A healthy worker's owner lease remains authoritative. Redis locks are
        # intentionally absent from this decision.
        if job.lease_until is not None and job.lease_until > now:
            continue
        if job.started_at is not None or job.lease_until is not None:
            job.state = PublishState.unknown
            job.finished_at = now
            job.response_json = {
                **dict(job.response_json or {}),
                "error": "publisher worker lease expired or external submission may have started; verify platform before force_retry",
                "recovery": "unknown",
            }
            job.lease_owner = None
            job.lease_until = None
            job.heartbeat_at = now
            if job.batch_id is not None:
                reconcile_publish_batch(db, job.batch_id)
            db.add(job)
            unknown += 1
            continue

        operation_key = str(job.operation_key or publish_operation_key(job.id))
        inbox = (
            db.query(OperationInbox)
            .filter(OperationInbox.operation_key == operation_key)
            .with_for_update(skip_locked=True)
            .one_or_none()
        )
        if inbox is not None:
            if inbox.status == "completed":
                continue
            inbox_lease_until = inbox.lease_until
            if inbox_lease_until is not None:
                inbox_lease_until = inbox_lease_until.replace(
                    tzinfo=inbox_lease_until.tzinfo or timezone.utc
                )
            if inbox.status == "processing":
                if inbox_lease_until is None or inbox_lease_until <= now:
                    job.state = PublishState.unknown
                    job.finished_at = now
                    job.response_json = {
                        **dict(job.response_json or {}),
                        "error": "expired publish operation lease; verify the platform before retry",
                        "recovery": "unknown",
                    }
                    job.lease_owner = None
                    job.lease_until = None
                    job.heartbeat_at = now
                    if job.batch_id is not None:
                        reconcile_publish_batch(db, job.batch_id)
                    db.add(job)
                    unknown += 1
                continue
            if inbox.status == "failed":
                continue
            retry_after = inbox.retry_after
            if retry_after is not None:
                retry_after = retry_after.replace(tzinfo=retry_after.tzinfo or timezone.utc)
            if inbox.status == "pending" and retry_after is not None and retry_after > now:
                continue

        event = (
            db.query(OutboxEvent)
            .filter(OutboxEvent.operation_key == operation_key)
            .order_by(OutboxEvent.created_at.desc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if event is not None:
            # A broker-accepted operation is not provably missing merely because
            # no worker has claimed it. The persistent dispatcher handles
            # pending or failed rows; accepted rows require verification.
            continue

        # Recover only legacy jobs missing their durable dispatch intent.
        enqueue_publish_job_dispatch(db, job, redeliver=False)
        requeued += 1

    db.flush()
    return PublishDispatchRecovery(requeued=requeued, unknown=unknown)


def publish_batch_has_target(
    batch: PublishBatch,
    platform: object,
    account_id: uuid.UUID | str | None,
) -> bool:
    expected_key = publish_target_key(platform, account_id)
    return any(
        str(target.get("key") or publish_target_key(target.get("platform"), target.get("account_id"))) == expected_key
        for target in list(batch.expected_targets or [])
    )


def publish_batch_target_keys(batch: PublishBatch) -> list[str]:
    return [
        str(target.get("key") or publish_target_key(target.get("platform"), target.get("account_id")))
        for target in list(batch.expected_targets or [])
    ]


def current_publish_batches_for_task(db: Session, task_id: uuid.UUID) -> list[PublishBatch]:
    """Return the newest batch for every channel/account target on a task.

    A task may publish to Bilibili and Douyin at the same time.  The old
    single ``Task.active_publish_batch_id`` pointer cannot represent that, so
    currentness is resolved independently for each target.
    """
    batches = (
        db.query(PublishBatch)
        .filter(PublishBatch.task_id == task_id)
        .order_by(PublishBatch.created_at.desc(), PublishBatch.id.desc())
        .all()
    )
    latest_by_target: dict[str, PublishBatch] = {}
    for batch in batches:
        for key in publish_batch_target_keys(batch):
            latest_by_target.setdefault(key, batch)

    unique: list[PublishBatch] = []
    seen: set[uuid.UUID] = set()
    for batch in latest_by_target.values():
        if batch.id not in seen:
            seen.add(batch.id)
            unique.append(batch)
    return unique


def latest_publish_batch_for_target(
    db: Session,
    task_id: uuid.UUID,
    *,
    platform: object,
    account_id: uuid.UUID | str | None,
) -> PublishBatch | None:
    key = publish_target_key(platform, account_id)
    for batch in current_publish_batches_for_task(db, task_id):
        if key in publish_batch_target_keys(batch):
            return batch
    return None


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


def evaluate_task_publish_batches(batches: list[PublishBatch]) -> PublishBatchEvaluation:
    """Aggregate the independent current channel batches into task status."""
    states = {str(batch.state or "") for batch in batches}
    if PublishBatchState.active.value in states:
        return PublishBatchEvaluation(PublishBatchState.active, TaskStatus.publishing, False)
    if states and states <= {PublishBatchState.succeeded.value}:
        return PublishBatchEvaluation(PublishBatchState.succeeded, TaskStatus.published, True)
    if PublishBatchState.succeeded.value in states or PublishBatchState.partial_failed.value in states:
        return PublishBatchEvaluation(PublishBatchState.partial_failed, TaskStatus.publishing, False)
    return PublishBatchEvaluation(PublishBatchState.failed, TaskStatus.failed, False)


def _lock_task_and_batch(db: Session, batch_id: uuid.UUID) -> tuple[Task, PublishBatch]:
    """Lock every publish transition in one order: Task, then PublishBatch."""
    batch_ref = db.get(PublishBatch, batch_id)
    if not batch_ref:
        raise ValueError("publish batch not found")
    task = db.get(Task, batch_ref.task_id, with_for_update=True)
    if not task:
        raise ValueError("publish batch task not found")
    batch = db.get(PublishBatch, batch_id, with_for_update=True)
    if not batch:
        raise ValueError("publish batch not found")
    if batch.task_id != task.id:
        raise ValueError("publish batch task changed while locking")
    return task, batch


def _reconcile_locked_publish_batch(
    db: Session,
    task: Task,
    batch: PublishBatch,
) -> PublishBatchReconciliation:

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
    previous_state = batch.state
    batch.state = evaluation.batch_state.value
    if evaluation.batch_state != PublishBatchState.active and (previous_state != batch.state or batch.finished_at is None):
        batch.finished_at = datetime.now(timezone.utc)

    db.add(batch)

    # Flush this batch before resolving the task's current batch per target.
    if evaluation.batch_state != PublishBatchState.active:
        from videoroll.apps.outbox.service import create_outbox_event

        create_outbox_event(
            db,
            event_type="workflow.publish.finished",
            aggregate_type="publish_batch",
            aggregate_id=batch.id,
            task_name="subtitle_service.push_publish_workflow_event",
            args={
                "args": [str(batch.id), evaluation.batch_state.value, str(task.id)],
                "queue": SUBTITLE_CONTROL_QUEUE,
            },
            operation_key=f"workflow-publish-finished:{batch.id}:{evaluation.batch_state.value}",
        )

    # Flush this batch before resolving the task's current batch per target.
    # The task may have several active channel batches at once.
    db.flush()
    current_batches = current_publish_batches_for_task(db, task.id)
    is_current_batch = any(current.id == batch.id for current in current_batches)
    task_evaluation = evaluate_task_publish_batches(current_batches)
    if is_current_batch and task.status != TaskStatus.canceled:
        task.status = task_evaluation.task_status
        if task_evaluation.batch_state == PublishBatchState.succeeded:
            task.error_code = None
            task.error_message = None
        elif task_evaluation.batch_state == PublishBatchState.partial_failed:
            task.error_code = "PUBLISH_PARTIAL_FAILURE"
            task.error_message = "one or more publish targets failed; retry only failed targets"
        elif task_evaluation.batch_state == PublishBatchState.failed:
            task.error_code = "PUBLISH_FAILED"
            task.error_message = "all publish targets failed"
        db.add(task)

    # Do not mark delivery before the configured Celery app has accepted the
    # task.  A duplicate cleanup is harmless; a lost cleanup is not.  The
    # durable event is inserted in this same transaction as the terminal batch
    # state; the marker itself is written only when cleanup completes.
    cleanup_needed = bool(is_current_batch and task_evaluation.cleanup_ready and batch.cleanup_enqueued_at is None)
    if cleanup_needed:
        from videoroll.apps.outbox.service import create_outbox_event

        create_outbox_event(
            db,
            event_type="publish.cleanup",
            aggregate_type="publish_batch",
            aggregate_id=batch.id,
            task_name="subtitle_service.cleanup_task",
            args={"args": [str(task.id), str(batch.id)], "queue": SUBTITLE_CONTROL_QUEUE},
            operation_key=f"publish-cleanup:{batch.id}",
            available_at=publish_cleanup_due_at(batch, current_batches),
        )

    return PublishBatchReconciliation(
        batch_id=batch.id,
        batch_state=evaluation.batch_state,
        task_status=task_evaluation.task_status,
        cleanup_needed=cleanup_needed,
    )


def reconcile_publish_batch(db: Session, batch_id: uuid.UUID) -> PublishBatchReconciliation:
    """Persist the aggregate state after one publisher updates a PublishJob."""
    task, batch = _lock_task_and_batch(db, batch_id)
    return _reconcile_locked_publish_batch(db, task, batch)


def record_publish_batch_dispatch_error(
    db: Session,
    batch_id: uuid.UUID,
    *,
    platform: object,
    account_id: uuid.UUID | str | None,
    detail: str,
) -> PublishBatchReconciliation:
    task, batch = _lock_task_and_batch(db, batch_id)
    outcomes = dict(batch.outcomes_json or {})
    outcomes[publish_target_key(platform, account_id)] = {"state": PublishState.failed.value, "detail": str(detail)}
    batch.outcomes_json = outcomes
    db.add(batch)
    return _reconcile_locked_publish_batch(db, task, batch)


def bind_unresolved_social_publish_target(
    db: Session,
    batch_id: uuid.UUID,
    *,
    platform: object,
    account_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Bind an accountless social target under the standard Task→Batch lock."""
    task, batch = _lock_task_and_batch(db, batch_id)
    platform_value = _value(platform)
    try:
        platform_enum = Platform(platform_value)
    except ValueError as exc:
        raise ValueError(f"unsupported publish platform: {platform_value}") from exc
    old_key = publish_target_key(platform_value, None)
    new_target = {
        "key": publish_target_key(platform_value, account_id),
        "platform": platform_value,
        "account_id": str(account_id),
    }
    expected_targets: list[dict[str, Any]] = []
    found = False
    for item in list(batch.expected_targets or []):
        key = str(item.get("key") or publish_target_key(item.get("platform"), item.get("account_id")))
        if key == old_key:
            expected_targets.append(dict(new_target))
            found = True
        else:
            expected_targets.append(dict(item))
    if not found:
        raise ValueError("unresolved publish target not found in batch")
    has_job = (
        db.query(PublishJob.id)
        .filter(
            PublishJob.batch_id == batch.id,
            PublishJob.platform == platform_enum,
            PublishJob.account_id.is_(None),
        )
        .first()
        is not None
    )
    if has_job:
        raise ValueError("unresolved publish target already has a job")
    batch.expected_targets = expected_targets
    outcomes = dict(batch.outcomes_json or {})
    outcomes.pop(old_key, None)
    batch.outcomes_json = outcomes
    db.add(batch)
    return new_target


def mark_publish_batch_cleanup_enqueued(db: Session, batch_id: uuid.UUID) -> bool:
    """Persist the terminal cleanup marker after cleanup work completes.

    The historical public name is retained for callers during the rolling
    deployment; durable delivery is now tracked by ``OutboxEvent`` instead.
    """
    try:
        task, batch = _lock_task_and_batch(db, batch_id)
    except ValueError:
        return False
    if batch.state != PublishBatchState.succeeded.value or batch.cleanup_enqueued_at is not None:
        return False
    if not any(current.id == batch.id for current in current_publish_batches_for_task(db, task.id)):
        return False
    batch.cleanup_enqueued_at = datetime.now(timezone.utc)
    batch.cleanup_delivery_version = 2
    db.add(batch)
    return True


def due_missing_publish_cleanup_batches(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[PublishBatch]:
    """Find only matured legacy cleanups without an outbox intent.

    Normal cleanup is already persisted during batch reconciliation. Never
    re-scan all future-dated events, and never fabricate a second cleanup
    event for a batch whose intent exists but has not reached its due time.
    """
    now = now or datetime.now(timezone.utc)
    event_exists = exists().where(
        OutboxEvent.event_type == "publish.cleanup",
        OutboxEvent.aggregate_type == "publish_batch",
        OutboxEvent.aggregate_id == cast(PublishBatch.id, String),
    )
    return (
        db.query(PublishBatch)
        .join(Task, Task.id == PublishBatch.task_id)
        .filter(
            PublishBatch.state == PublishBatchState.succeeded.value,
            PublishBatch.cleanup_enqueued_at.is_(None),
            PublishBatch.finished_at.isnot(None),
            PublishBatch.finished_at <= now - PUBLISH_CLEANUP_RETENTION,
            Task.status == TaskStatus.published,
            ~event_exists,
        )
        .order_by(PublishBatch.finished_at.asc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )


def enqueue_publish_batch_cleanup(
    db: Session,
    celery_app: Any,
    task_id: uuid.UUID,
    batch_id: uuid.UUID,
    *,
    needed: bool,
    wake_dispatch: bool = True,
) -> bool:
    """Persist cleanup dispatch and wake the shared dispatcher only when due.

    ``celery_app`` remains in the public signature for legacy callers. It is
    only used for a best-effort wake-up; the outbox row is the authoritative
    record and survives a broker outage. Future-dated cleanup events are left
    for the periodic dispatcher instead of creating useless broker messages.
    """
    if not needed:
        return False
    from videoroll.apps.outbox.service import create_outbox_event

    batch = db.get(PublishBatch, batch_id)
    if batch is None or batch.task_id != task_id or batch.state != PublishBatchState.succeeded.value:
        return False
    available_at = publish_cleanup_due_at(batch, current_publish_batches_for_task(db, task_id))
    event = create_outbox_event(
        db,
        event_type="publish.cleanup",
        aggregate_type="publish_batch",
        aggregate_id=batch_id,
        task_name="subtitle_service.cleanup_task",
        args={"args": [str(task_id), str(batch_id)], "queue": SUBTITLE_CONTROL_QUEUE},
        operation_key=f"publish-cleanup:{batch_id}",
        available_at=available_at,
    )
    db.commit()
    event_available_at = event.available_at
    if event_available_at is not None:
        event_available_at = event_available_at.replace(
            tzinfo=event_available_at.tzinfo or timezone.utc
        )
    if (
        wake_dispatch
        and event.status == "pending"
        and (event_available_at is None or event_available_at <= datetime.now(timezone.utc))
    ):
        try:
            celery_app.send_task("subtitle_service.dispatch_outbox", args=[], queue=SUBTITLE_CONTROL_QUEUE)
        except Exception:
            # The event is already committed and will be picked up by the periodic
            # dispatcher. Do not turn a durable pending event into a failed
            # domain transition merely because a broker wake-up is down.
            pass
    return event is not None
