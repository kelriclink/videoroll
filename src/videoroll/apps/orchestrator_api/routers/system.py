from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db
from videoroll.apps.orchestrator_api.schemas import SystemResourcesRead
from videoroll.apps.orchestrator_api.services.system_service import collect_system_resources


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/system/resources", response_model=SystemResourcesRead)
def system_resources(db: Session = Depends(get_db)) -> SystemResourcesRead:
    return collect_system_resources(db)
