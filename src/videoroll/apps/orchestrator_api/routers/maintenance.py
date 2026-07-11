from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_settings
from videoroll.apps.orchestrator_api.schemas import WorkdirMaintenanceRead
from videoroll.apps.orchestrator_api.services import maintenance_service
from videoroll.config import OrchestratorSettings


router = APIRouter()


@router.get("/maintenance/workdir", response_model=WorkdirMaintenanceRead)
def get_workdir_maintenance(
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> WorkdirMaintenanceRead:
    return maintenance_service.workdir_scan_to_read(maintenance_service.scan_workdir_state(settings, db))


@router.post("/maintenance/workdir/cleanup", response_model=WorkdirMaintenanceRead)
def cleanup_workdir_maintenance(
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> WorkdirMaintenanceRead:
    result = maintenance_service.cleanup_workdir(settings, db, owner_prefix="manual")
    if result is None:
        raise HTTPException(status_code=409, detail="workdir cleanup already running")
    return result
