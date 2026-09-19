from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db
from videoroll.apps.orchestrator_api.schemas import (
    AIUsagePricingRead,
    AIUsageSummaryRead,
    AlertRead,
    AlertReportRequest,
    AlertScanResponse,
)
from videoroll.apps.orchestrator_api.services import operations_service
from videoroll.config import get_orchestrator_settings


router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("/ai-usage", response_model=AIUsageSummaryRead)
def get_ai_usage(
    hours: int = Query(default=24, ge=1, le=2160),
    recent_limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> AIUsageSummaryRead:
    return AIUsageSummaryRead(**operations_service.ai_usage_summary(db, hours=hours, recent_limit=recent_limit))


@router.get("/ai-usage/pricing", response_model=AIUsagePricingRead)
def get_ai_usage_pricing(db: Session = Depends(get_db)) -> AIUsagePricingRead:
    return AIUsagePricingRead(**operations_service.get_ai_pricing(db))


@router.put("/ai-usage/pricing", response_model=AIUsagePricingRead)
def put_ai_usage_pricing(payload: AIUsagePricingRead, db: Session = Depends(get_db)) -> AIUsagePricingRead:
    try:
        result = operations_service.set_ai_pricing(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AIUsagePricingRead(**result)


@router.get("/alerts", response_model=list[AlertRead])
def get_alerts(
    status: str = Query(default="all"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[AlertRead]:
    if status not in {"all", "active", "open", "acknowledged", "resolved"}:
        raise HTTPException(status_code=400, detail="invalid alert status")
    return [AlertRead(**row) for row in operations_service.list_alerts(db, status=status, limit=limit)]


@router.post("/alerts/scan", response_model=AlertScanResponse)
def post_alert_scan(db: Session = Depends(get_db)) -> AlertScanResponse:
    return AlertScanResponse(**operations_service.scan_alerts(get_orchestrator_settings(), db))


@router.post("/alerts/report", response_model=AlertRead | None)
def post_alert_report(payload: AlertReportRequest, db: Session = Depends(get_db)) -> AlertRead | None:
    row = operations_service.report_alert(db, payload.model_dump())
    if row is None:
        return None
    return AlertRead(**operations_service._alert_dict(row))


@router.post("/alerts/{alert_id}/ack", response_model=AlertRead)
def acknowledge_alert(alert_id: uuid.UUID, db: Session = Depends(get_db)) -> AlertRead:
    row = operations_service.set_alert_status(db, alert_id, status="acknowledged")
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return AlertRead(**operations_service._alert_dict(row))


@router.post("/alerts/{alert_id}/resolve", response_model=AlertRead)
def resolve_alert(alert_id: uuid.UUID, db: Session = Depends(get_db)) -> AlertRead:
    row = operations_service.set_alert_status(db, alert_id, status="resolved")
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return AlertRead(**operations_service._alert_dict(row))
