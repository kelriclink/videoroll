from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db
from videoroll.apps.orchestrator_api.schemas import (
    AdminAuthLoginRequest,
    AdminAuthSetupRequest,
    AdminAuthStatusRead,
)
from videoroll.apps.orchestrator_api.services import auth_service


router = APIRouter()


@router.get("/auth/status", response_model=AdminAuthStatusRead)
def auth_status(request: Request, db: Session = Depends(get_db)) -> AdminAuthStatusRead:
    return auth_service.auth_status(request, db)


@router.post("/auth/setup", response_model=AdminAuthStatusRead)
def auth_setup(
    payload: AdminAuthSetupRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    return auth_service.setup_auth(payload, request, db)


@router.post("/auth/login", response_model=AdminAuthStatusRead)
def auth_login(
    payload: AdminAuthLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    return auth_service.login(payload, request, db)


@router.post("/auth/logout", response_model=AdminAuthStatusRead)
def auth_logout(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return auth_service.logout(request, db)
