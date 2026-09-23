from __future__ import annotations
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from videoroll.apps.orchestrator_api.render_worker_schemas import (
    ExecutionAdminActionRequest, ExecutionCompleteRequest, ExecutionFailRequest, ExecutionHeartbeatRequest, WorkerControlRequest, WorkerEnrollRequest, WorkerHeartbeatRequest, WorkerRegisterRequest,
)
from videoroll.apps.orchestrator_api.services import render_worker_service
from videoroll.db.base import Base
from videoroll.db.models import AppSetting, Asset, AssetKind, OutboxEvent, RenderExecution, RenderJob, RenderJobStatus, RenderWorker, RenderWorkerEnrollment, SourceLicense, SourceType, Task, TaskStatus

@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, Asset.__table__, RenderJob.__table__, RenderWorker.__table__, RenderExecution.__table__, RenderWorkerEnrollment.__table__, OutboxEvent.__table__, AppSetting.__table__])
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


def test_list_executions_can_filter_by_task(db: Session) -> None:
    worker = make_worker(db)
    first = make_job(db, priority=10)
    second = make_job(db, priority=0)
    first_execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    second_execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert first_execution is not None and second_execution is not None

    rows = render_worker_service.list_executions(db, task_id=first.task_id)

    assert [row.id for row in rows] == [first_execution.id]
    assert second_execution.id not in {row.id for row in rows}


def test_execution_heartbeat_without_progress_renews_only_execution_lease(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    original_job_lease = job.lease_until
    result = render_worker_service.heartbeat_execution(db, execution.id, ExecutionHeartbeatRequest(
        fence_token=execution.fence_token, progress=None, metrics={"device_id": "gpu0"},
    ))
    assert result.lease_until is not None
    db.refresh(job)
    assert job.lease_until == original_job_lease

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

    assert db.query(OutboxEvent).filter(OutboxEvent.event_type == "workflow.render.finished").count() == 0


def test_render_success_writes_durable_hatchet_event_outbox(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    output = Asset(
        task_id=job.task_id,
        kind=AssetKind.video_final,
        storage_key=f"final/{job.task_id}/video.mp4",
    )
    db.add(output); db.commit()

    render_worker_service.complete_execution(
        db,
        execution.id,
        ExecutionCompleteRequest(
            fence_token=execution.fence_token,
            output_asset_id=output.id,
            output={"fps": 90.0},
        ),
    )

    event = db.query(OutboxEvent).filter(OutboxEvent.event_type == "workflow.render.finished").one()
    assert event.aggregate_id == str(job.id)
    assert event.task_name == "subtitle_service.push_render_workflow_event"
    assert event.args_json["queue"] == "subtitle-control"
    assert event.args_json["args"][:3] == [str(job.id), "succeeded", str(execution.id)]


def test_nonretryable_render_failure_writes_terminal_hatchet_event(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None

    render_worker_service.fail_execution(
        db,
        execution.id,
        ExecutionFailRequest(
            fence_token=execution.fence_token,
            error="encoder permanently failed",
            retryable=False,
        ),
    )

    event = db.query(OutboxEvent).filter(OutboxEvent.event_type == "workflow.render.finished").one()
    assert event.args_json["queue"] == "subtitle-control"
    assert event.args_json["args"] == [
        str(job.id),
        "failed",
        str(execution.id),
        "encoder permanently failed",
    ]


def test_automatic_render_does_not_create_legacy_publish_outbox(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    # Completion reads the current runtime-profile flag. Set it after claim so
    # this test stays focused on the terminal outbox route instead of ASS rebuild.
    job.request_json = {**dict(job.request_json or {}), "runtime_profile": True}
    db.add(job); db.commit()
    output = Asset(task_id=job.task_id, kind=AssetKind.video_final, storage_key=f"final/{job.task_id}/video.mp4")
    db.add(output); db.commit()

    render_worker_service.complete_execution(
        db,
        execution.id,
        ExecutionCompleteRequest(fence_token=execution.fence_token, output_asset_id=output.id),
    )

    assert db.query(OutboxEvent).filter(OutboxEvent.event_type == "render.after_publish").count() == 0
    assert db.query(OutboxEvent).filter(OutboxEvent.event_type == "workflow.render.finished").count() == 1


def test_manual_after_render_publish_outbox_uses_control_queue(db: Session) -> None:
    worker = make_worker(db); job = make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    job.request_json = {
        **dict(job.request_json or {}),
        "runtime_profile": False,
        "after_render": {"publish": True, "publish_payload": {"platforms": ["bilibili"]}},
    }
    db.add(job); db.commit()
    output = Asset(task_id=job.task_id, kind=AssetKind.video_final, storage_key=f"final/{job.task_id}/video.mp4")
    db.add(output); db.commit()

    render_worker_service.complete_execution(
        db,
        execution.id,
        ExecutionCompleteRequest(fence_token=execution.fence_token, output_asset_id=output.id),
    )

    publish_event = db.query(OutboxEvent).filter(OutboxEvent.event_type == "render.after_publish").one()
    assert publish_event.task_name == "subtitle_service.after_render_publish"
    assert publish_event.args_json["queue"] == "subtitle-control"

def test_expired_execution_is_reconciled_and_releases_worker_capacity(db: Session) -> None:
    worker = make_worker(db); make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    execution.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.add(execution); db.commit()
    assert render_worker_service.reconcile_worker_executions(db, worker.id) == 1
    db.commit(); db.refresh(execution)
    assert execution.state == "lost"


def test_worker_heartbeat_does_not_reconcile_execution_lease(db: Session) -> None:
    worker = make_worker(db); make_job(db)
    execution, _ = render_worker_service.claim_job(db, store(), worker.id, ["http"])
    assert execution is not None
    execution.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.add(execution); db.commit()
    render_worker_service.heartbeat_worker(
        db,
        worker.id,
        WorkerHeartbeatRequest(status="busy", resources={"devices": []}),
    )
    db.refresh(execution)
    assert execution.state in {"claimed", "running"}


def test_worker_admin_capacity_uses_node_management_limit(db: Session) -> None:
    worker = make_worker(db)
    worker.max_concurrency = 4
    worker.active_jobs = 1
    worker.resources = {
        "devices": [
            {"id": "gpu0", "active_jobs": 1},
            {"id": "gpu1", "active_jobs": 0},
        ]
    }
    db.add(worker); db.commit()
    payload = render_worker_service.worker_admin_payload(worker)
    assert payload["device_count"] == 2
    assert payload["detected_capacity"] == 2
    assert payload["effective_capacity"] == 4
    assert payload["available_slots"] == 3


def test_remote_reenrollment_preserves_admin_concurrency(db: Session) -> None:
    existing = make_worker(db)
    existing.max_concurrency = 1
    db.add(existing); db.commit()
    _enrollment, token = render_worker_service.create_enrollment(db, label="re-pair", ttl_minutes=30)
    enrolled, _credential = render_worker_service.enroll_worker(
        db,
        WorkerEnrollRequest(
            worker_key=existing.worker_key,
            name=existing.name,
            platform=existing.platform,
            architecture=existing.architecture,
            capabilities={"encoders": ["av1_nvenc"]},
            resources={"device_count": 2},
            max_concurrency=2,
            enrollment_token=token,
        ),
    )
    assert enrolled.id == existing.id
    assert enrolled.max_concurrency == 1


def test_worker_key_can_be_changed_but_must_remain_unique(db: Session) -> None:
    worker = make_worker(db)
    updated = render_worker_service.control_worker(
        db,
        worker.id,
        WorkerControlRequest(worker_key=" recovered-a380 "),
    )
    assert updated.worker_key == "recovered-a380"

    other = render_worker_service.register_worker(db, WorkerRegisterRequest(
        worker_key="other-node", name="Other", platform="linux", capabilities={},
    ))
    with pytest.raises(Exception) as exc:
        render_worker_service.control_worker(
            db,
            other.id,
            WorkerControlRequest(worker_key="recovered-a380"),
        )
    assert "already in use" in str(exc.value)


def test_deleted_worker_can_be_recreated_with_same_worker_key(db: Session) -> None:
    worker = make_worker(db)
    original_id = worker.id
    key = worker.worker_key
    db.delete(worker)
    db.commit()

    _enrollment, token = render_worker_service.create_enrollment(
        db, label="recover deleted node", ttl_minutes=30
    )
    recovered, credential = render_worker_service.enroll_worker(
        db,
        WorkerEnrollRequest(
            worker_key=key,
            name="Windows 4060 recovered",
            platform="windows",
            architecture="amd64",
            capabilities={"encoders": ["av1_nvenc"]},
            resources={"device_count": 1},
            max_concurrency=2,
            enrollment_token=token,
        ),
    )
    assert recovered.id != original_id
    assert recovered.worker_key == key
    assert credential.startswith(render_worker_service.WORKER_CREDENTIAL_PREFIX)
    assert render_worker_service.authenticate_worker(db, credential).id == recovered.id


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
    assert "fence_token" not in payload


def test_claim_respects_current_free_device_encoders(db: Session) -> None:
    worker = make_worker(db)
    av1_job = make_job(db, priority=10, codec="av1")
    h264_job = make_job(db, priority=0, codec="h264")
    execution, _ = render_worker_service.claim_job(
        db, store(), worker.id, ["http"], available_encoders=["h264_nvenc"],
    )
    assert execution is not None
    assert execution.render_job_id == h264_job.id
    assert db.get(RenderJob, av1_job.id).status == RenderJobStatus.queued

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


def test_worker_limit_four_allows_four_concurrent_claims(db: Session) -> None:
    worker = make_worker(db)
    worker.max_concurrency = 4
    db.add(worker); db.commit()
    jobs = [make_job(db, priority=10 - index) for index in range(5)]

    claimed = [
        render_worker_service.claim_job(db, store(), worker.id, ["http"])[0]
        for _ in range(4)
    ]
    fifth, spec = render_worker_service.claim_job(db, store(), worker.id, ["http"])

    assert all(execution is not None for execution in claimed)
    assert {execution.render_job_id for execution in claimed if execution is not None} == {
        job.id for job in jobs[:4]
    }
    assert fifth is None and spec is None
    assert db.get(RenderJob, jobs[4].id).status == RenderJobStatus.queued


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


def test_local_worker_enrollment_preserves_existing_admin_concurrency(db: Session) -> None:
    from videoroll.apps.orchestrator_api.render_worker_schemas import LocalWorkerEnrollRequest

    existing = render_worker_service.ensure_local_worker(
        db, capabilities={"encoders": ["av1_vaapi"]}, resources={}
    )
    existing.max_concurrency = 4
    db.add(existing)
    db.commit()
    enrolled, _credential = render_worker_service.enroll_local_worker(
        db,
        LocalWorkerEnrollRequest(
            worker_key="local-render",
            name="本机渲染节点",
            platform="linux",
            capabilities={"encoders": ["av1_vaapi"]},
            max_concurrency=1,
        ),
    )
    assert enrolled.id == existing.id
    assert enrolled.max_concurrency == 4
