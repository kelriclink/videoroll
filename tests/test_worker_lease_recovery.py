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
    recover_expired_leases,
    release_job_lease,
)
from videoroll.db.base import Base
from videoroll.db.models import (
    RenderJob,
    RenderJobStatus,
    PublishJob,
    SourceLicense,
    SourceType,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Task.__table__, SubtitleJob.__table__, RenderJob.__table__, PublishJob.__table__],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(
            engine,
            tables=[PublishJob.__table__, RenderJob.__table__, SubtitleJob.__table__, Task.__table__],
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


def test_running_job_without_a_lease_is_not_recovered(db: Session) -> None:
    job = SubtitleJob(task_id=_task(db).id, status=SubtitleJobStatus.running)
    db.add(job)
    db.flush()

    summary = recover_expired_leases(db, now=_now(), limit=100)

    assert job.status == SubtitleJobStatus.running
    assert summary.total_recovered == 0


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
