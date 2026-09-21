from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.subtitle_service.worker_concurrency import (
    acquire_job_lease,
    heartbeat_job_lease,
    live_leased_task_ids,
    recover_expired_leases,
    release_job_lease,
)
from videoroll.db.base import Base
from videoroll.db.models import (
    RenderExecution,
    RenderJob,
    RenderJobStatus,
    RenderWorker,
    PublishJob,
    SourceLicense,
    SourceType,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Task.__table__,
            SubtitleJob.__table__,
            RenderJob.__table__,
            RenderWorker.__table__,
            RenderExecution.__table__,
            PublishJob.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(
            engine,
            tables=[
                PublishJob.__table__,
                RenderExecution.__table__,
                RenderWorker.__table__,
                RenderJob.__table__,
                SubtitleJob.__table__,
                Task.__table__,
            ],
        )


def _task(db: Session) -> Task:
    task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
    db.add(task)
    db.flush()
    return task


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_live_subtitle_lease_is_not_requeued_by_recovery(db: Session) -> None:
    job = SubtitleJob(
        task_id=_task(db).id,
        status=SubtitleJobStatus.running,
        lease_owner="worker-a",
        lease_until=_now() + timedelta(minutes=2),
    )
    db.add(job)
    db.flush()

    summary = recover_expired_leases(db, now=_now(), limit=100)

    assert job.status == SubtitleJobStatus.running
    assert job.lease_owner == "worker-a"
    assert summary.subtitle_requeued == 0


def test_expired_render_lease_is_requeued_with_resume(db: Session) -> None:
    job = RenderJob(
        task_id=_task(db).id,
        status=RenderJobStatus.running,
        progress=58,
        retry_count=2,
        lease_owner="dead-worker",
        lease_until=_now() - timedelta(seconds=1),
    )
    db.add(job)
    db.flush()

    summary = recover_expired_leases(db, now=_now(), limit=100)

    assert job.status == RenderJobStatus.queued
    assert job.progress == 0
    assert job.retry_count == 3
    assert job.lease_owner is None
    assert job.lease_until is None
    assert summary.render_requeued == 1


def test_recent_execution_grace_prevents_immediate_render_requeue(db: Session) -> None:
    now = _now()
    task = _task(db)
    worker = RenderWorker(worker_key="worker-a", name="Worker A", platform="linux")
    job = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.running,
        lease_owner="render-worker:pending",
        lease_until=now - timedelta(seconds=5),
    )
    db.add_all([worker, job]); db.flush()
    execution = RenderExecution(
        render_job_id=job.id,
        worker_id=worker.id,
        attempt=1,
        fence_token="a" * 48,
        state="running",
        render_spec={},
        capability_snapshot={},
        lease_until=now - timedelta(seconds=5),
        heartbeat_at=now - timedelta(seconds=10),
    )
    db.add(execution); db.flush()

    summary = recover_expired_leases(db, now=now, limit=100)

    assert summary.render_requeued == 0
    assert job.status == RenderJobStatus.running
    assert execution.state == "running"


def test_stale_execution_is_fenced_before_render_requeue(db: Session) -> None:
    now = _now()
    task = _task(db)
    worker = RenderWorker(worker_key="worker-a", name="Worker A", platform="linux")
    job = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.running,
        lease_owner="render-worker:pending",
        lease_until=now - timedelta(minutes=2),
    )
    db.add_all([worker, job]); db.flush()
    execution = RenderExecution(
        render_job_id=job.id,
        worker_id=worker.id,
        attempt=1,
        fence_token="b" * 48,
        state="running",
        render_spec={},
        capability_snapshot={},
        lease_until=now - timedelta(minutes=2),
        heartbeat_at=now - timedelta(minutes=2),
    )
    db.add(execution); db.flush()

    summary = recover_expired_leases(db, now=now, limit=100)

    assert summary.render_requeued == 1
    assert job.status == RenderJobStatus.queued
    assert execution.state == "lost"


def test_recovery_reconciles_published_render_after_lease_expiry(db: Session) -> None:
    task = _task(db)
    task.status = TaskStatus.published
    job = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.running,
        progress=58,
        retry_count=2,
        lease_owner="dead-worker",
        lease_until=_now() - timedelta(seconds=1),
    )
    db.add(job)
    db.flush()

    summary = recover_expired_leases(db, now=_now(), limit=100)

    assert job.status == RenderJobStatus.canceled
    assert job.retry_count == 2
    assert summary.render_requeued == 0
    assert summary.terminal_render_reconciled == 1


def test_recovery_message_is_not_appended_twice(db: Session) -> None:
    detail = "Worker lease expired while rendering; requeued for resume."
    job = RenderJob(
        task_id=_task(db).id,
        status=RenderJobStatus.running,
        lease_owner="dead-worker",
        lease_until=_now() - timedelta(seconds=1),
        error_message=detail,
    )
    db.add(job)
    db.flush()

    summary = recover_expired_leases(db, now=_now(), limit=100)

    assert summary.render_requeued == 1
    assert job.error_message == detail


def test_live_job_lease_reserves_task_until_recovery(db: Session) -> None:
    live_task = _task(db)
    live = RenderJob(
        task_id=live_task.id,
        status=RenderJobStatus.running,
        lease_owner="worker-a",
        lease_until=_now() + timedelta(minutes=5),
    )
    expired = SubtitleJob(
        task_id=_task(db).id,
        status=SubtitleJobStatus.running,
        lease_owner="dead-worker",
        lease_until=_now() - timedelta(seconds=1),
    )
    db.add_all([live, expired])
    db.flush()

    assert live_leased_task_ids(db, _now()) == {live_task.id}


def test_live_render_execution_reserves_task_when_job_lease_is_stale(db: Session) -> None:
    now = _now()
    task = _task(db)
    worker = RenderWorker(worker_key="worker-live", name="Worker Live", platform="linux")
    job = RenderJob(
        task_id=task.id,
        status=RenderJobStatus.running,
        lease_owner="render-worker:live",
        lease_until=now - timedelta(seconds=5),
    )
    db.add_all([worker, job]); db.flush()
    execution = RenderExecution(
        render_job_id=job.id,
        worker_id=worker.id,
        attempt=1,
        fence_token="c" * 48,
        state="running",
        render_spec={},
        capability_snapshot={},
        lease_until=now + timedelta(minutes=2),
        heartbeat_at=now,
    )
    db.add(execution); db.flush()

    assert live_leased_task_ids(db, now) == {task.id}


def test_running_job_without_a_lease_is_not_recovered(db: Session) -> None:
    job = SubtitleJob(task_id=_task(db).id, status=SubtitleJobStatus.running)
    db.add(job)
    db.flush()

    summary = recover_expired_leases(db, now=_now(), limit=100)

    assert job.status == SubtitleJobStatus.running
    assert summary.total_recovered == 0


def test_stale_legacy_running_job_without_lease_is_requeued(db: Session) -> None:
    now = _now()
    job = SubtitleJob(
        task_id=_task(db).id,
        status=SubtitleJobStatus.running,
        updated_at=now - timedelta(hours=3),
    )
    db.add(job)
    db.flush()

    summary = recover_expired_leases(db, now=now, limit=100)

    assert job.status == SubtitleJobStatus.queued
    assert job.request_json.get("resume") is True
    assert summary.subtitle_requeued == 1
    assert summary.legacy_subtitle_requeued == 1


def test_job_lease_is_owned_and_heartbeat_cannot_renew_after_expiry(db: Session) -> None:
    job = SubtitleJob(task_id=_task(db).id, status=SubtitleJobStatus.running)
    db.add(job)
    db.flush()

    assert acquire_job_lease(db, job, "worker-a", ttl_seconds=60) is True
    db.commit()
    assert acquire_job_lease(db, job, "worker-b", ttl_seconds=60) is False
    assert heartbeat_job_lease(db, job.id, "worker-b", ttl_seconds=60) is False

    job.lease_until = _now() - timedelta(seconds=1)
    db.commit()
    assert heartbeat_job_lease(db, job.id, "worker-a", ttl_seconds=60) is False

    summary = recover_expired_leases(db, now=_now(), limit=100)
    assert summary.subtitle_requeued == 1
    assert release_job_lease(db, job.id, "worker-a") is False
