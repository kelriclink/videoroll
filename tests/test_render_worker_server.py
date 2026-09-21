from __future__ import annotations
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from videoroll.apps.orchestrator_api.render_worker_schemas import (
    ExecutionAdminActionRequest, ExecutionFailRequest, ExecutionHeartbeatRequest, WorkerRegisterRequest,
)
from videoroll.apps.orchestrator_api.services import render_worker_service
from videoroll.db.base import Base
from videoroll.db.models import Asset, RenderExecution, RenderJob, RenderJobStatus, RenderWorker, SourceLicense, SourceType, Task, TaskStatus

@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, Asset.__table__, RenderJob.__table__, RenderWorker.__table__, RenderExecution.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()

def make_worker(db: Session) -> RenderWorker:
    return render_worker_service.register_worker(db, WorkerRegisterRequest(
        worker_key="win-4060", name="Windows 4060", platform="windows", architecture="amd64",
        capabilities={"encoders":["av1_nvenc","h264_nvenc"],"filters":["overlay_cuda"]}, max_concurrency=2,
    ))

def make_job(db: Session, *, priority: int = 0, codec: str = "av1") -> RenderJob:
    task = Task(source_type=SourceType.local, source_license=SourceLicense.own, status=TaskStatus.subtitle_ready, priority=priority)
    db.add(task); db.flush()
    job = RenderJob(task_id=task.id, status=RenderJobStatus.queued, request_json={
        "input_key":"raw/video.mp4", "srt_key":"subtitle/sub.srt", "burn_in":True,
        "render":{"video_codec":codec},
    })
    db.add(job); db.commit(); return job

def store() -> Mock:
    value = Mock()
    value.head_object.return_value = {"ContentLength": 1234}
    return value

def test_claim_uses_existing_priority_and_fences_execution(db: Session) -> None:
    worker = make_worker(db)
    low = make_job(db, priority=0)
    high = make_job(db, priority=10)
    execution, spec = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None and spec is not None
    assert execution.render_job_id == high.id
    assert spec.schema_version == 1
    assert {item.role for item in spec.artifacts} == {"input","srt"}
    db.refresh(high)
    assert high.status == RenderJobStatus.running
    assert high.lease_owner == f"render-worker:{execution.id}"
    assert db.get(RenderJob, low.id).status == RenderJobStatus.queued

def test_execution_heartbeat_renews_job_and_execution_lease(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    old_until = execution.lease_until
    result = render_worker_service.heartbeat_execution(db, execution.id, ExecutionHeartbeatRequest(
        fence_token=execution.fence_token, progress=37, metrics={"fps":91.2},
    ))
    assert result.state == "running" and result.progress == 37
    assert result.lease_until is not None and old_until is not None and result.lease_until >= old_until
    db.refresh(job)
    assert job.progress == 37 and job.lease_until == result.lease_until

def test_stale_fence_token_cannot_mutate_execution(db: Session) -> None:
    worker = make_worker(db); make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    with pytest.raises(Exception) as exc:
        render_worker_service.heartbeat_execution(db, execution.id, ExecutionHeartbeatRequest(
            fence_token="0"*48, progress=50,
        ))
    assert "fence token" in str(exc.value)

def test_retryable_failure_requeues_existing_render_job(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    render_worker_service.fail_execution(db, execution.id, ExecutionFailRequest(
        fence_token=execution.fence_token, error="worker disconnected", retryable=True,
    ))
    db.refresh(job)
    assert job.status == RenderJobStatus.queued
    assert job.retry_count == 1
    assert job.lease_owner is None

def test_expired_execution_is_reconciled_and_releases_worker_capacity(db: Session) -> None:
    worker = make_worker(db); make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    execution.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.add(execution); db.commit()
    assert render_worker_service.reconcile_worker_executions(db, worker.id) == 1
    db.commit(); db.refresh(execution)
    assert execution.state == "lost"

def test_admin_cancel_fences_running_execution(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    render_worker_service.cancel_execution(db, execution.id, "operator canceled")
    db.refresh(job); db.refresh(execution)
    assert execution.state == "canceled"
    assert job.status == RenderJobStatus.canceled
    assert render_worker_service.cancellation_state(db, execution.id)[0] is True
    with pytest.raises(Exception):
        render_worker_service.heartbeat_execution(db, execution.id, ExecutionHeartbeatRequest(
            fence_token=execution.fence_token, progress=80,
        ))

def test_admin_requeue_creates_clean_claimable_job(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    first, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert first is not None
    render_worker_service.requeue_execution(db, first.id, "move to another node")
    db.refresh(job)
    assert job.status == RenderJobStatus.queued and job.progress == 0
    second, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert second is not None
    assert second.attempt == first.attempt + 1
    assert second.fence_token != first.fence_token

def test_execution_admin_payload_exposes_scheduler_fields(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    payload = render_worker_service.execution_admin_payload(db, execution)
    assert payload["worker_name"] == "Windows 4060"
    assert payload["task_id"] == job.task_id
    assert payload["job_status"] == "running"

def test_worker_max_concurrency_is_enforced_by_coordinator(db: Session) -> None:
    worker = make_worker(db)
    worker.max_concurrency = 1
    db.add(worker); db.commit()
    first_job = make_job(db, priority=10)
    second_job = make_job(db, priority=0)
    first, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert first is not None and first.render_job_id == first_job.id
    second, spec = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert second is None and spec is None
    assert db.get(RenderJob, second_job.id).status == RenderJobStatus.queued


def test_local_worker_preserves_admin_concurrency_setting(db: Session) -> None:
    worker = render_worker_service.ensure_local_worker(
        db, capabilities={"encoders": ["av1_vaapi"]}, resources={}
    )
    worker.max_concurrency = 4
    db.add(worker); db.commit()
    same = render_worker_service.ensure_local_worker(
        db, capabilities={"encoders": ["av1_vaapi", "h264_vaapi"]}, resources={}
    )
    assert same.id == worker.id
    assert same.max_concurrency == 4
    assert same.labels.get("local") is True
