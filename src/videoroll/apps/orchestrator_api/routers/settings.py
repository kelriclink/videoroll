from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_settings
from videoroll.apps.orchestrator_api.remote_api_settings_store import (
    get_remote_api_settings,
    update_remote_api_settings,
)
from videoroll.apps.orchestrator_api.schemas import (
    PublishReviewSettingsRead,
    PublishReviewSettingsUpdate,
    RemoteAPISettingsRead,
    RemoteAPISettingsUpdate,
    StorageRetentionSettingsRead,
    StorageRetentionSettingsUpdate,
    YouTubeSettingsRead,
    YouTubeSettingsUpdate,
)
from videoroll.apps.orchestrator_api.storage_retention_store import (
    get_storage_retention_settings,
    update_storage_retention_settings,
)
from videoroll.apps.publish_review_store import get_publish_review_settings, update_publish_review_settings
from videoroll.apps.youtube_settings_store import get_youtube_settings, update_youtube_settings
from videoroll.config import OrchestratorSettings


router = APIRouter()


def _youtube_cookie_file_status(settings: OrchestratorSettings) -> tuple[bool, bool]:
    cookie_file = str(settings.youtube_cookie_file or "").strip()
    if not cookie_file:
        return False, False
    try:
        return True, Path(cookie_file).is_file()
    except Exception:
        return True, False


@router.get("/settings/storage", response_model=StorageRetentionSettingsRead)
def get_storage_settings(db: Session = Depends(get_db)) -> StorageRetentionSettingsRead:
    return StorageRetentionSettingsRead(**get_storage_retention_settings(db))


@router.put("/settings/storage", response_model=StorageRetentionSettingsRead)
def put_storage_settings(
    payload: StorageRetentionSettingsUpdate,
    db: Session = Depends(get_db),
) -> StorageRetentionSettingsRead:
    try:
        config = update_storage_retention_settings(db, payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StorageRetentionSettingsRead(**config)


@router.get("/settings/api", response_model=RemoteAPISettingsRead)
def get_remote_api_settings_view(db: Session = Depends(get_db)) -> RemoteAPISettingsRead:
    return RemoteAPISettingsRead(**get_remote_api_settings(db))


@router.put("/settings/api", response_model=RemoteAPISettingsRead)
def put_remote_api_settings_view(
    payload: RemoteAPISettingsUpdate,
    db: Session = Depends(get_db),
) -> RemoteAPISettingsRead:
    return RemoteAPISettingsRead(**update_remote_api_settings(db, payload.model_dump(exclude_unset=True)))


@router.get("/settings/review", response_model=PublishReviewSettingsRead)
def get_publish_review_settings_view(db: Session = Depends(get_db)) -> PublishReviewSettingsRead:
    return PublishReviewSettingsRead(**get_publish_review_settings(db))


@router.put("/settings/review", response_model=PublishReviewSettingsRead)
def put_publish_review_settings_view(
    payload: PublishReviewSettingsUpdate,
    db: Session = Depends(get_db),
) -> PublishReviewSettingsRead:
    try:
        config = update_publish_review_settings(db, payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PublishReviewSettingsRead(**config)


@router.get("/settings/youtube", response_model=YouTubeSettingsRead)
def get_youtube_settings_view(
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> YouTubeSettingsRead:
    config = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    configured, exists = _youtube_cookie_file_status(settings)
    return YouTubeSettingsRead(**config, cookie_file_configured=configured, cookie_file_exists=exists)


@router.put("/settings/youtube", response_model=YouTubeSettingsRead)
def put_youtube_settings_view(
    payload: YouTubeSettingsUpdate,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> YouTubeSettingsRead:
    try:
        config = update_youtube_settings(
            db,
            payload.model_dump(exclude_unset=True),
            default_proxy=settings.youtube_proxy,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    configured, exists = _youtube_cookie_file_status(settings)
    return YouTubeSettingsRead(**config, cookie_file_configured=configured, cookie_file_exists=exists)
