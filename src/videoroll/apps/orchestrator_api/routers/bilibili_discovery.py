"""Dashboard-only public Bilibili discovery; authenticated by orchestrator middleware."""
from __future__ import annotations

from fastapi import APIRouter, Query

from videoroll.apps.orchestrator_api.services.bilibili_discovery import get_bilibili_discovery


router = APIRouter(prefix="/bilibili/discovery", tags=["bilibili"])


@router.get("")
def bilibili_discovery(refresh: bool = Query(False)) -> dict:
    return get_bilibili_discovery(refresh=refresh)
