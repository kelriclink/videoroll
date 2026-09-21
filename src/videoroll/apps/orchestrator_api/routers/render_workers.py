from __future__ import annotations
import uuid
from pathlib import Path
import secrets
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session
from videoroll.apps.orchestrator_api.dependencies import get_db, get_settings, get_store
from videoroll.apps.orchestrator_api.render_worker_schemas import (
    ClaimRequest, ClaimResponse, ExecutionAdminActionRequest, ExecutionAdminRead, ExecutionCancelRead, ExecutionCompleteRequest, ExecutionFailRequest,
    ExecutionHeartbeatRequest, ExecutionLogRequest, ExecutionProgressRequest, ExecutionRead,
    LocalWorkerEnrollRequest, WorkerEnrollRequest, WorkerEnrollResponse, WorkerHeartbeatRequest, WorkerRead,
)
from videoroll.apps.orchestrator_api.schemas import AssetRead
from videoroll.apps.orchestrator_api.services import asset_service, render_worker_service
from videoroll.config import OrchestratorSettings
from videoroll.db.models import Asset, AssetKind, RenderJob, RenderWorker, Task
from videoroll.storage.filesystem import FileStore, StorageObjectNotFound

router = APIRouter(prefix="/render-workers/v1", tags=["render-workers"])

def require_worker(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> RenderWorker:
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "render worker credential required")
    return render_worker_service.authenticate_worker(db, token)

def require_assigned_worker(worker_id: uuid.UUID, worker: RenderWorker = Depends(require_worker)) -> RenderWorker:
    if worker.id != worker_id: raise HTTPException(403, "worker credential does not match worker")
    return worker

def require_execution_worker(
    execution_id: uuid.UUID,
    db: Session = Depends(get_db),
    worker: RenderWorker = Depends(require_worker),
) -> RenderWorker:
    execution = render_worker_service.get_execution(db, execution_id)
    if execution.worker_id != worker.id: raise HTTPException(403, "execution is assigned to another worker")
    return worker

@router.get("/protocol")
def protocol() -> dict[str, object]:
    return {
        "protocol_version": 1,
        "render_spec_versions": [1],
        "transfer_modes": ["http"],
        "features": [
            "worker_registration", "capability_report", "pull_claim", "lease_heartbeat",
            "progress", "log_tail", "range_download", "output_upload", "fencing",
            "retryable_failure", "cancel_poll",
        ],
        "lease_seconds": render_worker_service.LEASE_SECONDS,
    }

@router.post("/enroll", response_model=WorkerEnrollResponse)
def enroll(payload: WorkerEnrollRequest, db: Session = Depends(get_db)) -> WorkerEnrollResponse:
    worker, credential = render_worker_service.enroll_worker(db, payload)
    return WorkerEnrollResponse(worker=WorkerRead.model_validate(worker), credential=credential)

@router.post("/local-enroll", response_model=WorkerEnrollResponse)
def local_enroll(
    payload: LocalWorkerEnrollRequest,
    request: Request,
    x_internal_secret: str = Header(default="", alias="X-Internal-Secret"),
    db: Session = Depends(get_db),
    settings: OrchestratorSettings = Depends(get_settings),
) -> WorkerEnrollResponse:
    # Bootstrap is restricted to the coordinator host/private Compose network.
    # Remote machines must use a one-time enrollment token.
    if request.client is None or not render_worker_service.is_private_worker_bootstrap_address(request.client.host):
        raise HTTPException(403, "local render worker enrollment is restricted to private coordinator networks")
    if not x_internal_secret or not secrets.compare_digest(x_internal_secret, settings.admin_bootstrap_secret):
        raise HTTPException(401, "local render worker bootstrap secret is invalid")
    worker, credential = render_worker_service.enroll_local_worker(db, payload)
    return WorkerEnrollResponse(worker=WorkerRead.model_validate(worker), credential=credential)

@router.post("/{worker_id}/heartbeat", response_model=WorkerRead)
def heartbeat(worker_id: uuid.UUID, payload: WorkerHeartbeatRequest, db: Session = Depends(get_db), _worker: RenderWorker = Depends(require_assigned_worker)) -> RenderWorker:
    return render_worker_service.heartbeat_worker(db, worker_id, payload)

@router.post("/{worker_id}/claim", response_model=ClaimResponse)
def claim(worker_id: uuid.UUID, payload: ClaimRequest, db: Session = Depends(get_db), store: FileStore = Depends(get_store), _worker: RenderWorker = Depends(require_assigned_worker)) -> ClaimResponse:
    if payload.available_slots <= 0:
        return ClaimResponse()
    execution, spec = render_worker_service.claim_job(
        db, store, worker_id, list(payload.accepted_transfer_modes),
        available_encoders=list(payload.available_encoders),
    )
    return ClaimResponse(execution=execution, render_spec=spec)

@router.post("/executions/{execution_id}/heartbeat", response_model=ExecutionRead)
def execution_heartbeat(execution_id: uuid.UUID, payload: ExecutionHeartbeatRequest, db: Session = Depends(get_db), _worker: RenderWorker = Depends(require_execution_worker)):
    return render_worker_service.heartbeat_execution(db, execution_id, payload)

@router.post("/executions/{execution_id}/progress", response_model=ExecutionRead)
def execution_progress(execution_id: uuid.UUID, payload: ExecutionProgressRequest, db: Session = Depends(get_db), _worker: RenderWorker = Depends(require_execution_worker)):
    return render_worker_service.heartbeat_execution(db, execution_id, payload)

@router.post("/executions/{execution_id}/logs", response_model=ExecutionRead)
def execution_logs(execution_id: uuid.UUID, payload: ExecutionLogRequest, db: Session = Depends(get_db), _worker: RenderWorker = Depends(require_execution_worker)):
    return render_worker_service.append_log(db, execution_id, payload.fence_token, payload.text)

@router.get("/executions/{execution_id}/cancel", response_model=ExecutionCancelRead)
def execution_cancel(execution_id: uuid.UUID, db: Session = Depends(get_db), _worker: RenderWorker = Depends(require_execution_worker)) -> ExecutionCancelRead:
    requested, reason = render_worker_service.cancellation_state(db, execution_id)
    return ExecutionCancelRead(cancel_requested=requested, reason=reason)

@router.get("/executions/{execution_id}/artifacts/{role}")
def download_artifact(execution_id: uuid.UUID, role: str, request: Request, db: Session = Depends(get_db), store: FileStore = Depends(get_store), _worker: RenderWorker = Depends(require_execution_worker)) -> Response:
    execution = render_worker_service.get_execution(db, execution_id)
    key = render_worker_service.artifact_key_for_role(execution, role)
    try:
        obj = store.get_object(key, range_bytes=request.headers.get("range"))
    except StorageObjectNotFound as exc:
        raise HTTPException(404, "render artifact missing") from exc
    body = obj["Body"]
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(obj.get("ContentLength") or 0), "X-Content-Type-Options": "nosniff"}
    if obj.get("ContentRange"): headers["Content-Range"] = str(obj["ContentRange"])
    status = 206 if obj.get("ContentRange") else 200
    return StreamingResponse(FileStore.iter_body(body), status_code=status, media_type="application/octet-stream", headers=headers)

@router.post("/executions/{execution_id}/output", response_model=AssetRead)
async def upload_output(
    execution_id: uuid.UUID,
    file: UploadFile = File(...),
    fence_token: str = Form(...),
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
    _worker: RenderWorker = Depends(require_execution_worker),
) -> Asset:
    execution = render_worker_service.get_execution(db, execution_id, lock=True)
    render_worker_service.validate_fence(execution, fence_token)
    job = db.get(RenderJob, execution.render_job_id)
    if job is None: raise HTTPException(404, "render job not found")
    task = db.get(Task, job.task_id)
    if task is None: raise HTTPException(404, "task not found")
    execution.state = "uploading"; db.add(execution); db.commit()
    return await asset_service.store_uploaded_task_asset(
        task=task, file=file, store=store, db=db, temp_prefix="render_worker_",
        default_suffix=".mp4", key_prefix="final", object_name_prefix="video_remote",
        asset_kind=AssetKind.video_final, max_bytes=256 * 1024 * 1024 * 1024,
    )

@router.post("/executions/{execution_id}/complete", response_model=ExecutionRead)
def complete(execution_id: uuid.UUID, payload: ExecutionCompleteRequest, db: Session = Depends(get_db), _worker: RenderWorker = Depends(require_execution_worker)):
    return render_worker_service.complete_execution(db, execution_id, payload)

@router.post("/executions/{execution_id}/fail", response_model=ExecutionRead)
def fail(execution_id: uuid.UUID, payload: ExecutionFailRequest, db: Session = Depends(get_db), _worker: RenderWorker = Depends(require_execution_worker)):
    return render_worker_service.fail_execution(db, execution_id, payload)
