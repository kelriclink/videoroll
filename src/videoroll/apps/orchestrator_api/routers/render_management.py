from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db
from videoroll.apps.orchestrator_api.render_worker_schemas import (
    EnrollmentAdminRead, EnrollmentCreateRequest, EnrollmentCreateResponse,
    RenderConnectionUpdate,
    ExecutionAdminActionRequest, ExecutionAdminRead, WorkerAdminRead, WorkerControlRequest,
)
from videoroll.apps.orchestrator_api.services import render_worker_service

router = APIRouter(prefix="/render-management", tags=["render-management"])

def _server_url(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
    proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    return f"{proto}://{forwarded}".rstrip("/")

@router.get("/connection")
def connection(request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    return {"server_url": render_worker_service.get_server_url(db, _server_url(request)), "worker_api_path": "/api/render-workers/v1", "protocol_version": 1}

@router.put("/connection")
def update_connection(payload: RenderConnectionUpdate, db: Session = Depends(get_db)) -> dict[str, object]:
    return {"server_url": render_worker_service.set_server_url(db, payload.server_url), "worker_api_path": "/api/render-workers/v1", "protocol_version": 1}

@router.get("/enrollments", response_model=list[EnrollmentAdminRead])
def enrollments(db: Session = Depends(get_db)):
    return render_worker_service.list_enrollments(db)

@router.post("/enrollments", response_model=EnrollmentCreateResponse)
def create_enrollment(payload: EnrollmentCreateRequest, request: Request, db: Session = Depends(get_db)):
    row, token = render_worker_service.create_enrollment(db, label=payload.label, ttl_minutes=payload.ttl_minutes)
    return EnrollmentCreateResponse(id=row.id, token=token, server_url=render_worker_service.get_server_url(db, _server_url(request)), expires_at=row.expires_at)

@router.delete("/enrollments/{enrollment_id}", response_model=EnrollmentAdminRead)
def revoke_enrollment(enrollment_id: uuid.UUID, db: Session = Depends(get_db)):
    return render_worker_service.revoke_enrollment(db, enrollment_id)

@router.get("/workers", response_model=list[WorkerAdminRead])
def workers(db: Session = Depends(get_db)):
    return [render_worker_service.worker_admin_payload(row) for row in render_worker_service.list_workers(db)]

@router.patch("/workers/{worker_id}", response_model=WorkerAdminRead)
def control_worker(worker_id: uuid.UUID, payload: WorkerControlRequest, db: Session = Depends(get_db)):
    row = render_worker_service.control_worker(db, worker_id, payload)
    return render_worker_service.worker_admin_payload(row)

@router.post("/workers/{worker_id}/revoke-credential", response_model=WorkerAdminRead)
def revoke_worker(worker_id: uuid.UUID, db: Session = Depends(get_db)):
    row = render_worker_service.revoke_worker_credential(db, worker_id)
    return render_worker_service.worker_admin_payload(row)

@router.get("/executions", response_model=list[ExecutionAdminRead])
def executions(
    worker_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    state: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return [
        render_worker_service.execution_admin_payload(db, row)
        for row in render_worker_service.list_executions(
            db,
            worker_id=worker_id,
            task_id=task_id,
            state=state,
            limit=limit,
        )
    ]

@router.get("/executions/{execution_id}", response_model=ExecutionAdminRead)
def execution(execution_id: uuid.UUID, db: Session = Depends(get_db)):
    return render_worker_service.execution_admin_payload(db, render_worker_service.get_execution(db, execution_id))

@router.post("/executions/{execution_id}/cancel", response_model=ExecutionAdminRead)
def cancel(execution_id: uuid.UUID, payload: ExecutionAdminActionRequest, db: Session = Depends(get_db)):
    row = render_worker_service.cancel_execution(db, execution_id, payload.reason)
    return render_worker_service.execution_admin_payload(db, row)

@router.post("/executions/{execution_id}/requeue", response_model=ExecutionAdminRead)
def requeue(execution_id: uuid.UUID, payload: ExecutionAdminActionRequest, db: Session = Depends(get_db)):
    row = render_worker_service.requeue_execution(db, execution_id, payload.reason)
    return render_worker_service.execution_admin_payload(db, row)
