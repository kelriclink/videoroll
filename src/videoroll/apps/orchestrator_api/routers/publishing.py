from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_s3, get_settings
from videoroll.apps.orchestrator_api.schemas import (
    PublishActionRequest,
    PublishJobSummary,
    PublishMetaDraftRequest,
    PublishMetaDraftResponse,
    PublishPlatformSettingUpdate,
    PublishPlatformSettingsRead,
    PublishReviewActionRequest,
    RemotePublishResponse,
    TaskPublishReviewRead,
)
from videoroll.apps.orchestrator_api.services import publishing_service
from videoroll.config import OrchestratorSettings
from videoroll.storage.s3 import S3Store


router = APIRouter()


@router.get("/tasks/{task_id}/publish_meta")
def get_task_publish_meta(task_id: uuid.UUID, db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)) -> dict[str, Any]:
    return publishing_service.get_task_publish_meta(task_id, db, s3)


@router.get("/tasks/{task_id}/publish_meta/draft", response_model=PublishMetaDraftResponse)
def get_task_publish_meta_draft(
    task_id: uuid.UUID, db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)
) -> PublishMetaDraftResponse:
    return PublishMetaDraftResponse(meta=publishing_service.get_task_publish_meta_draft(task_id, db, s3))


@router.post("/tasks/{task_id}/publish_meta/draft", response_model=PublishMetaDraftResponse)
def generate_task_publish_meta_draft(
    task_id: uuid.UUID,
    payload: PublishMetaDraftRequest,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> PublishMetaDraftResponse:
    meta = publishing_service.generate_task_publish_meta_draft(
        task_id, mode=payload.mode, base_meta=payload.meta, db=db, s3=s3
    )
    return PublishMetaDraftResponse(meta=meta)


@router.put("/tasks/{task_id}/publish_meta")
def put_task_publish_meta(
    task_id: uuid.UUID, meta: dict[str, Any], db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)
) -> dict[str, Any]:
    return publishing_service.put_task_publish_meta(task_id, meta, db, s3)


@router.get("/tasks/{task_id}/publish_review", response_model=TaskPublishReviewRead)
def get_task_publish_review(task_id: uuid.UUID, db: Session = Depends(get_db)) -> TaskPublishReviewRead:
    return TaskPublishReviewRead(**publishing_service.get_task_publish_review(task_id, db))


@router.post("/tasks/{task_id}/actions/publish_review", response_model=TaskPublishReviewRead)
def run_task_publish_review(
    task_id: uuid.UUID,
    payload: PublishReviewActionRequest,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> TaskPublishReviewRead:
    return TaskPublishReviewRead(**publishing_service.review_task_publish(task_id, payload.meta, db, s3))


@router.get("/tasks/{task_id}/publish_jobs", response_model=list[PublishJobSummary])
def list_task_publish_jobs(
    task_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return publishing_service.list_task_publish_jobs(task_id, limit, db)


@router.get("/settings/publish/platforms", response_model=PublishPlatformSettingsRead)
def read_publish_platform_settings(db: Session = Depends(get_db)) -> PublishPlatformSettingsRead:
    return PublishPlatformSettingsRead(platforms=publishing_service.read_publish_platform_settings(db))


@router.put("/settings/publish/platforms/{platform}", response_model=PublishPlatformSettingsRead)
def put_publish_platform_setting(
    platform: str, payload: PublishPlatformSettingUpdate, db: Session = Depends(get_db)
) -> PublishPlatformSettingsRead:
    return PublishPlatformSettingsRead(
        platforms=publishing_service.put_publish_platform_setting(platform, payload.enabled, db)
    )


@router.get("/settings/publish/social/accounts")
def list_social_publish_accounts(
    platform: str | None = None, settings: OrchestratorSettings = Depends(get_settings)
) -> Any:
    return publishing_service.list_social_publish_accounts(platform, settings)


@router.post("/settings/publish/social/login-sessions/{platform}")
def start_social_login_session(
    platform: str, payload: dict[str, Any], settings: OrchestratorSettings = Depends(get_settings)
) -> Any:
    return publishing_service.start_social_login_session(platform, payload, settings)


@router.get("/settings/publish/social/login-sessions/{session_id}")
def get_social_login_session(
    session_id: uuid.UUID, settings: OrchestratorSettings = Depends(get_settings)
) -> Any:
    return publishing_service.get_social_login_session(session_id, settings)


@router.delete("/settings/publish/social/login-sessions/{session_id}")
def cancel_social_login_session(
    session_id: uuid.UUID, settings: OrchestratorSettings = Depends(get_settings)
) -> Any:
    return publishing_service.cancel_social_login_session(session_id, settings)


@router.post("/settings/publish/social/accounts/{platform}")
async def import_social_publish_account(
    platform: str,
    account_name: str = Form(...),
    file: UploadFile = File(...),
    settings: OrchestratorSettings = Depends(get_settings),
) -> Any:
    return await publishing_service.import_social_publish_account(platform, account_name, file, settings)


@router.post("/settings/publish/social/accounts/{account_id}/check")
def check_social_publish_account(
    account_id: uuid.UUID, settings: OrchestratorSettings = Depends(get_settings)
) -> Any:
    return publishing_service.check_social_publish_account(account_id, settings)


@router.delete("/settings/publish/social/accounts/{account_id}")
def delete_social_publish_account(
    account_id: uuid.UUID, settings: OrchestratorSettings = Depends(get_settings)
) -> Any:
    return publishing_service.delete_social_publish_account(account_id, settings)


@router.post("/tasks/{task_id}/actions/publish", response_model=RemotePublishResponse)
def enqueue_publish_job(
    task_id: uuid.UUID,
    payload: PublishActionRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> RemotePublishResponse:
    return publishing_service.enqueue_publish_job(task_id, payload, settings, db, s3)
