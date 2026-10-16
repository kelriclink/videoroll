from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.render_worker_schemas import (
    ExecutionCompleteRequest, ExecutionFailRequest, WorkerEnrollRequest, WorkerHeartbeatRequest, WorkerRegisterRequest,
)
from videoroll.apps.orchestrator_api.services import render_worker_service as service
from videoroll.apps.render_worker import runtime
from videoroll.db.base import Base
from videoroll.db.models import (
    AppSetting, Asset, AssetKind, OutboxEvent, RenderExecution, RenderJob, RenderJobStatus,
    RenderWorker, RenderWorkerEnrollment, SourceLicense, SourceType, Task, TaskStatus,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine, tables=[
            Task.__table__, Asset.__table__, RenderJob.__table__, RenderWorker.__table__,
            RenderExecution.__table__, RenderWorkerEnrollment.__table__, OutboxEvent.__table__,
            AppSetting.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_vaapi_discovery_checks_pci_vendor_and_each_actual_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime, "_drm_pci_identity", lambda path: (
        (0x8086, 0x56A5, "0000:01:00.0") if path.name == "renderD128" else (0x10DE, 0x1DB5, "0000:02:00.0")
    ))

    def probe(_ffmpeg: str, encoder: str, *, device_path: str, **_kwargs: object) -> bool:
        calls.append((device_path, encoder))
        return encoder == "av1_vaapi"

    monkeypatch.setattr(runtime, "_hardware_encoder_available", probe)
    assert runtime._vaapi_device(Path("/dev/dri/renderD129"), {"av1_vaapi"}, ffmpeg_path="ffmpeg") is None
    card = runtime._vaapi_device(
        Path("/dev/dri/renderD128"), {"av1_vaapi", "h264_vaapi"}, ffmpeg_path="ffmpeg",
    )
    assert card is not None
    assert card.encoders == ("av1_vaapi",)
    assert card.id == "vaapi:0000:01:00.0"
    assert "8086:56a5" in card.name
    assert calls == [
        ("/dev/dri/renderD128", "av1_vaapi"),
        ("/dev/dri/renderD128", "h264_vaapi"),
    ]


def test_device_quarantine_and_disable_only_block_new_assignments(monkeypatch: pytest.MonkeyPatch) -> None:
    device_a = runtime.RenderDevice(id="vaapi:renderD128", name="A380", backend="vaapi", encoders=("av1_vaapi",))
    device_b = runtime.RenderDevice(id="nvidia:GPU-v100", name="V100", backend="nvidia", encoders=("h264_nvenc",))
    monkeypatch.setattr(runtime, "probe_capabilities", lambda _cfg: ({}, {}, [device_a, device_b]))
    worker = runtime.RenderWorkerRuntime.__new__(runtime.RenderWorkerRuntime)
    import threading
    worker.devices = [device_a, device_b]
    worker._active_lock = threading.Lock()
    worker._disabled_device_ids = set()
    worker._device_cooldowns = {}
    worker._device_failures = {}
    worker._quarantine_device(device_a)
    assert [x.id for x in worker._eligible_devices()] == [device_b.id]
    worker._disabled_device_ids.add(device_b.id)
    assert worker._eligible_devices() == []
    worker._disabled_device_ids.clear()
    worker._device_cooldowns[device_a.id] = 0
    assert {x.id for x in worker._eligible_devices()} == {device_a.id, device_b.id}


def test_failed_device_is_skipped_when_another_compatible_gpu_exists() -> None:
    from videoroll.apps.orchestrator_api.render_worker_schemas import RenderSpec
    a380 = runtime.RenderDevice(
        id="vaapi:renderD128", name="A380", backend="vaapi", encoders=("h264_vaapi",),
    )
    v100 = runtime.RenderDevice(
        id="nvidia:GPU-v100", name="V100", backend="nvidia", encoders=("h264_nvenc",),
    )
    worker = runtime.RenderWorkerRuntime.__new__(runtime.RenderWorkerRuntime)
    worker.devices = [a380, v100]
    worker._active = {}
    worker._disabled_device_ids = set()
    worker._device_cooldowns = {}
    spec = RenderSpec(
        render_job_id=uuid.uuid4(), task_id=uuid.uuid4(), mode="burn_in",
        request={"render": {"video_codec": "h264"}},
    )
    worker._failed_devices_by_job = {spec.render_job_id: {v100.id}}
    assert worker._select_device_for_job(spec).id == a380.id
    worker._disabled_device_ids.add(a380.id)
    # No compatible alternative remains, so reuse the remaining GPU within
    # the coordinator's strict overall retry budget.
    assert worker._select_device_for_job(spec).id == v100.id


def _worker(db: Session) -> RenderWorker:
    return service.register_worker(db, WorkerRegisterRequest(
        worker_key="link", name="link", platform="linux",
        capabilities={"encoders": ["h264_vaapi"], "devices": [
            {"id": "vaapi:renderD128", "encoders": ["h264_vaapi"]},
        ]},
        resources={"devices": [{"id": "vaapi:renderD128", "status": "idle", "active_jobs": 0}]},
    ))


def _job(db: Session) -> RenderJob:
    task = Task(source_type=SourceType.local, source_license=SourceLicense.own, status=TaskStatus.subtitle_ready)
    db.add(task)
    db.flush()
    job = RenderJob(task_id=task.id, status=RenderJobStatus.queued, request_json={
        "input_key": "raw/video.mp4", "srt_key": "subtitle/sub.srt", "burn_in": True,
        "render": {"video_codec": "h264"},
    })
    db.add(job)
    db.commit()
    return job


def test_device_controls_persist_and_survive_heartbeat(db: Session) -> None:
    worker = _worker(db)
    service.control_worker_device(db, worker.id, "vaapi:renderD128", False)
    state = service.worker_admin_payload(worker)
    device = state["resources"]["devices"][0]
    assert device["enabled"] is False and device["status"] == "disabled"
    service.heartbeat_worker(db, worker.id, WorkerHeartbeatRequest(
        status="online", resources={"devices": [{"id": "vaapi:renderD128", "status": "idle"}]},
    ))
    db.refresh(worker)
    assert worker.labels[service.DISABLED_DEVICE_LABEL] == ["vaapi:renderD128"]
    service.control_worker_device(db, worker.id, "vaapi:renderD128", True)
    assert service.worker_admin_payload(worker)["resources"]["devices"][0]["enabled"] is True
    with pytest.raises(Exception, match="render device not found"):
        service.control_worker_device(db, worker.id, "vaapi:bogus", False)


def test_device_disable_survives_worker_reenrollment(db: Session) -> None:
    worker = _worker(db)
    service.control_worker_device(db, worker.id, "vaapi:renderD128", False)
    _enrollment, token = service.create_enrollment(db, label="recover GPU worker", ttl_minutes=30)
    recovered, _credential = service.enroll_worker(db, WorkerEnrollRequest(
        worker_key="link", name="link rebooted", platform="linux",
        capabilities={"encoders": ["h264_vaapi"], "devices": [
            {"id": "vaapi:renderD128", "encoders": ["h264_vaapi"]},
        ]},
        resources={"devices": [{"id": "vaapi:renderD128", "status": "idle"}]},
        enrollment_token=token,
    ))
    assert recovered.id == worker.id
    assert recovered.labels[service.DISABLED_DEVICE_LABEL] == ["vaapi:renderD128"]
    assert service.worker_admin_payload(recovered)["resources"]["devices"][0]["enabled"] is False


def test_retries_are_bounded_and_each_execution_history_is_preserved(db: Session) -> None:
    worker = _worker(db)
    job = _job(db)
    store = Mock()
    store.head_object.return_value = {"ContentLength": 16}
    all_executions = []
    for attempt in range(1, service.MAX_RENDER_ATTEMPTS + 1):
        execution, spec = service.claim_job(db, store, worker.id, ["http"])
        assert execution is not None and spec is not None
        assert execution.attempt == attempt
        all_executions.append(execution.id)
        service.fail_execution(db, execution.id, ExecutionFailRequest(
            fence_token=execution.fence_token, error="VAAPI unavailable", retryable=True,
        ))
    db.refresh(job)
    assert job.status == RenderJobStatus.failed
    assert job.retry_count == service.MAX_RENDER_ATTEMPTS - 1
    assert db.query(RenderExecution).filter(RenderExecution.render_job_id == job.id).count() == 5
    assert all(db.get(RenderExecution, item).error_message == "VAAPI unavailable" for item in all_executions)
    assert service.claim_job(db, store, worker.id, ["http"])[0] is None


def test_success_clears_previous_failure_without_losing_attempts(db: Session) -> None:
    worker = _worker(db)
    job = _job(db)
    store = Mock()
    store.head_object.return_value = {"ContentLength": 16}
    old, _ = service.claim_job(db, store, worker.id, ["http"])
    assert old is not None
    service.fail_execution(db, old.id, ExecutionFailRequest(
        fence_token=old.fence_token, error="temporary GPU reset", retryable=True,
    ))
    new, _ = service.claim_job(db, store, worker.id, ["http"])
    assert new is not None
    output = Asset(task_id=job.task_id, kind=AssetKind.video_final, storage_key="final/video.mp4")
    db.add(output)
    db.commit()
    new.output_json = {"uploaded_output_asset_id": str(output.id)}
    db.add(new); db.commit()
    service.complete_execution(db, new.id, ExecutionCompleteRequest(
        fence_token=new.fence_token, output_asset_id=output.id,
    ))
    db.refresh(job)
    assert job.status == RenderJobStatus.succeeded and job.error_message is None
    assert db.get(RenderExecution, old.id).error_message == "temporary GPU reset"
