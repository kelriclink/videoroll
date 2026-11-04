from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from videoroll.apps.publish_lifecycle import (
    PublishDispatchRecovery,
    enqueue_publish_job_dispatch,
    publish_operation_key,
    recover_stale_publish_dispatches,
)
from videoroll.db.models import Platform, PublishState


def _recovery_query(rows: list[MagicMock]) -> MagicMock:
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.with_for_update.return_value = query
    query.limit.return_value = query
    query.all.return_value = rows
    return query


def _recovery_single_query(value: MagicMock | None) -> MagicMock:
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.with_for_update.return_value = query
    query.one_or_none.return_value = value
    query.first.return_value = value
    return query


def test_publish_operation_key_is_stable_per_job() -> None:
    job_id = uuid.uuid4()

    assert publish_operation_key(job_id) == publish_operation_key(str(job_id))
    assert publish_operation_key(job_id) != publish_operation_key(uuid.uuid4())


def test_new_publish_job_creates_a_matching_durable_dispatch_event() -> None:
    job_id = uuid.uuid4()
    job = MagicMock(id=job_id, operation_key=None, platform=Platform.bilibili)
    db = MagicMock()

    with patch("videoroll.apps.outbox.service.create_outbox_event") as create_event:
        enqueue_publish_job_dispatch(db, job)

    assert job.operation_key == f"publish-job:{job_id}"
    create_event.assert_called_once_with(
        db,
        event_type="publish.dispatch",
        aggregate_type="publish_job",
        aggregate_id=job_id,
        task_name="bilibili_publisher.process_job",
        args={"args": [str(job_id)], "queue": "publish"},
        operation_key=job.operation_key,
    )


def test_recovery_creates_missing_dispatch_event_for_unstarted_job() -> None:
    job_id = uuid.uuid4()
    job = MagicMock(
        id=job_id,
        operation_key=f"publish-job:{job_id}",
        state=PublishState.submitting,
        started_at=None,
        lease_until=None,
        heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        batch_id=None,
    )
    db = MagicMock()
    db.query.side_effect = [
        _recovery_query([job]),
        _recovery_single_query(None),
        _recovery_single_query(None),
    ]

    with patch("videoroll.apps.publish_lifecycle.enqueue_publish_job_dispatch") as enqueue:
        result = recover_stale_publish_dispatches(db, now=datetime.now(timezone.utc))

    assert result == PublishDispatchRecovery(requeued=1, unknown=0)
    enqueue.assert_called_once_with(db, job, redeliver=False)
    assert job.state == PublishState.submitting


def test_recovery_does_not_redeliver_a_recently_dispatched_queued_job() -> None:
    now = datetime.now(timezone.utc)
    job_id = uuid.uuid4()
    job = MagicMock(
        id=job_id,
        operation_key=f"publish-job:{job_id}",
        state=PublishState.submitting,
        started_at=None,
        lease_until=None,
        batch_id=None,
    )
    event = MagicMock(
        status="dispatched",
        delivered_at=now - timedelta(days=60),
        heartbeat_at=None,
        updated_at=now,
    )
    db = MagicMock()
    db.query.side_effect = [
        _recovery_query([job]),
        _recovery_single_query(None),
        _recovery_single_query(event),
    ]

    with patch("videoroll.apps.publish_lifecycle.enqueue_publish_job_dispatch") as enqueue:
        result = recover_stale_publish_dispatches(db, now=now)

    assert result == PublishDispatchRecovery(requeued=0, unknown=0)
    enqueue.assert_not_called()


def test_recovery_never_redelivers_an_old_broker_accepted_dispatch() -> None:
    now = datetime.now(timezone.utc)
    job_id = uuid.uuid4()
    job = MagicMock(
        id=job_id,
        operation_key=f"publish-job:{job_id}",
        state=PublishState.submitting,
        started_at=None,
        lease_until=None,
        batch_id=None,
    )
    event = MagicMock(
        status="dispatched",
        delivered_at=now - timedelta(days=90),
        heartbeat_at=None,
        updated_at=now - timedelta(days=90),
    )
    db = MagicMock()
    db.query.side_effect = [
        _recovery_query([job]),
        _recovery_single_query(None),
        _recovery_single_query(event),
    ]

    with patch("videoroll.apps.publish_lifecycle.enqueue_publish_job_dispatch") as enqueue:
        result = recover_stale_publish_dispatches(db, now=now)

    assert result == PublishDispatchRecovery(requeued=0, unknown=0)
    enqueue.assert_not_called()


def test_recovery_does_not_redeliver_with_a_live_operation_inbox_lease() -> None:
    now = datetime.now(timezone.utc)
    job_id = uuid.uuid4()
    job = MagicMock(
        id=job_id,
        operation_key=f"publish-job:{job_id}",
        state=PublishState.submitting,
        started_at=None,
        lease_until=None,
        batch_id=None,
    )
    inbox = MagicMock(status="processing", lease_until=now + timedelta(minutes=10), retry_after=None)
    db = MagicMock()
    db.query.side_effect = [_recovery_query([job]), _recovery_single_query(inbox)]

    with patch("videoroll.apps.publish_lifecycle.enqueue_publish_job_dispatch") as enqueue:
        result = recover_stale_publish_dispatches(db, now=now)

    assert result == PublishDispatchRecovery(requeued=0, unknown=0)
    enqueue.assert_not_called()


def test_recovery_marks_expired_or_started_work_unknown_not_retryable() -> None:
    now = datetime.now(timezone.utc)
    expired = MagicMock(
        state=PublishState.submitting,
        started_at=None,
        lease_until=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(minutes=1),
        batch_id=None,
        response_json={},
    )
    started = MagicMock(
        state=PublishState.submitting,
        started_at=now - timedelta(seconds=1),
        lease_until=None,
        heartbeat_at=None,
        batch_id=None,
        response_json={},
    )
    db = MagicMock()
    db.query.return_value = _recovery_query([expired, started])

    result = recover_stale_publish_dispatches(db, now=now)

    assert result == PublishDispatchRecovery(requeued=0, unknown=2)
    assert expired.state == PublishState.unknown
    assert started.state == PublishState.unknown
    assert expired.response_json["recovery"] == "unknown"
    assert started.response_json["recovery"] == "unknown"
