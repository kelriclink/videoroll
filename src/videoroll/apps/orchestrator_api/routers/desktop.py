from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db
from videoroll.apps.orchestrator_api.desktop_grants import (
    DesktopGrantCreate,
    DesktopGrantRead,
    authorize_desktop_request,
    create_desktop_grant,
)


router = APIRouter()


def _admin_session(request: Request) -> str:
    session = str(getattr(request.state, "admin_session", "") or "").strip()
    if not session:
        raise HTTPException(status_code=401, detail="administrator session is required")
    return session


@router.post("/desktop/grants", response_model=DesktopGrantRead)
def create_grant(
    payload: DesktopGrantCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> DesktopGrantRead:
    try:
        return create_desktop_grant(
            db,
            _admin_session(request),
            payload.desktop_type,
            payload.resource_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/desktop/authorize", include_in_schema=False, status_code=204)
def authorize_grant(request: Request, db: Session = Depends(get_db)) -> Response:
    request.state.desktop_grant_db = db
    authorize_desktop_request(request)
    return Response(status_code=204)
