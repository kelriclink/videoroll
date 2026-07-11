from __future__ import annotations

import os
import uuid
from typing import Generator

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from videoroll.apps.publish_gateway import SUPPORTED_SOCIAL_PLATFORMS, normalize_publish_platform, normalize_social_publish_meta
from videoroll.apps.social_publisher.account_store import (
    MAX_STORAGE_STATE_BYTES,
    account_read,
    canonicalize_storage_state,
    disable_account,
    upsert_account,
)
from videoroll.apps.social_publisher.login_sessions import BrowserLoginManager, BrowserLoginSession
from videoroll.apps.social_publisher.schemas import (
    SocialAccountImportResponse,
    SocialAccountRead,
    SocialLoginSessionRead,
    SocialLoginStartRequest,
    SocialPublishRequest,
    SocialPublishResponse,
)
from videoroll.apps.social_publisher.worker import celery_app
from videoroll.config import SocialPublisherSettings, get_social_publisher_settings
from videoroll.db.auto_migrate import auto_migrate
from videoroll.db.base import Base
from videoroll.db.models import Account, Platform, PublishJob, PublishState, Task, TaskStatus
from videoroll.db.session import db_session, get_engine


def get_settings() -> SocialPublisherSettings:
    return get_social_publisher_settings()


def get_db(settings: SocialPublisherSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


app = FastAPI(title="videoroll-social-publisher", version="0.1.0")
login_manager = BrowserLoginManager(get_social_publisher_settings())
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        item.strip()
        for item in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
        if item.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    settings = get_social_publisher_settings()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    auto_migrate(settings.database_url)


@app.get("/health")
def health(settings: SocialPublisherSettings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "driver": "sau"}


def _login_session_read(session: BrowserLoginSession) -> SocialLoginSessionRead:
    return SocialLoginSessionRead(
        id=session.id,
        platform=session.platform,
        account_name=session.account_name,
        state=session.state,
        message=session.message,
        browser_url=session.browser_url,
        created_at=session.created_at,
        finished_at=session.finished_at,
    )


@app.post("/login-sessions/{platform}", response_model=SocialLoginSessionRead)
def start_login_session(platform: str, payload: SocialLoginStartRequest) -> SocialLoginSessionRead:
    try:
        return _login_session_read(login_manager.start(platform, payload.account_name))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/login-sessions/{session_id}", response_model=SocialLoginSessionRead)
def get_login_session(session_id: uuid.UUID) -> SocialLoginSessionRead:
    session = login_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="login session not found")
    return _login_session_read(session)


@app.delete("/login-sessions/{session_id}", response_model=SocialLoginSessionRead)
def cancel_login_session(session_id: uuid.UUID) -> SocialLoginSessionRead:
    session = login_manager.cancel(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="login session not found")
    return _login_session_read(session)


@app.get("/accounts", response_model=list[SocialAccountRead])
def list_accounts(platform: str | None = None, db: Session = Depends(get_db)) -> list[SocialAccountRead]:
    query = db.query(Account).filter(Account.platform.in_([Platform.douyin, Platform.xiaohongshu, Platform.kuaishou]))
    if platform:
        value = normalize_publish_platform(platform)
        if value not in SUPPORTED_SOCIAL_PLATFORMS:
            raise HTTPException(status_code=400, detail="unsupported social platform")
        query = query.filter(Account.platform == Platform(value))
    return [account_read(account) for account in query.order_by(Account.platform, Account.name).all()]


@app.post("/accounts/{platform}", response_model=SocialAccountImportResponse)
async def import_account(
    platform: str,
    account_name: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> SocialAccountImportResponse:
    value = normalize_publish_platform(platform)
    if value not in SUPPORTED_SOCIAL_PLATFORMS:
        raise HTTPException(status_code=400, detail="unsupported social platform")
    raw = await file.read(MAX_STORAGE_STATE_BYTES + 1)
    try:
        existing_account = (
            db.query(Account)
            .filter(Account.platform == Platform(value), Account.name == account_name.strip())
            .with_for_update()
            .one_or_none()
        )
        if existing_account and (
            existing_account.check_state == "checking"
            or db.query(PublishJob.id)
            .filter(PublishJob.account_id == existing_account.id, PublishJob.state == PublishState.submitting)
            .first()
        ):
            raise HTTPException(status_code=409, detail="account is busy; wait for the current check or publish job")
        canonical = canonicalize_storage_state(raw)
        account = upsert_account(db, value, account_name, canonical)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = celery_app.send_task("social_publisher.check_account", args=[str(account.id)], queue="social_publish")
    return SocialAccountImportResponse(account=account_read(account), check_job_id=str(result.id))


@app.post("/accounts/{account_id}/check", response_model=SocialAccountImportResponse)
def recheck_account(account_id: uuid.UUID, db: Session = Depends(get_db)) -> SocialAccountImportResponse:
    account = db.get(Account, account_id, with_for_update=True)
    if not account or account.platform not in {Platform.douyin, Platform.xiaohongshu, Platform.kuaishou}:
        raise HTTPException(status_code=404, detail="social account not found")
    if not account.is_active or not account.secrets_encrypted:
        raise HTTPException(status_code=400, detail="social account is inactive")
    if account.check_state == "checking" or db.query(PublishJob.id).filter(
        PublishJob.account_id == account.id,
        PublishJob.state == PublishState.submitting,
    ).first():
        raise HTTPException(status_code=409, detail="account is busy; wait for the current check or publish job")
    account.check_state = "queued"
    account.last_check_message = None
    db.add(account)
    db.commit()
    result = celery_app.send_task("social_publisher.check_account", args=[str(account.id)], queue="social_publish")
    return SocialAccountImportResponse(account=account_read(account), check_job_id=str(result.id))


@app.delete("/accounts/{account_id}")
def delete_account(account_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, bool]:
    account = db.get(Account, account_id, with_for_update=True)
    if not account or account.platform not in {Platform.douyin, Platform.xiaohongshu, Platform.kuaishou}:
        raise HTTPException(status_code=404, detail="social account not found")
    if account.check_state == "checking" or db.query(PublishJob.id).filter(
        PublishJob.account_id == account.id,
        PublishJob.state == PublishState.submitting,
    ).first():
        raise HTTPException(status_code=409, detail="account is busy; wait for the current check or publish job")
    disable_account(db, account)
    return {"ok": True}


def _response_from_job(job: PublishJob) -> SocialPublishResponse:
    return SocialPublishResponse(
        job_id=job.id,
        platform=job.platform.value,
        state=job.state.value,
        external_id=job.external_id,
        external_url=job.external_url,
        response=job.response_json,
    )


@app.post("/sau/{platform}/publish", response_model=SocialPublishResponse)
def publish(
    platform: str,
    payload: SocialPublishRequest,
    settings: SocialPublisherSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SocialPublishResponse:
    value = normalize_publish_platform(platform)
    if value not in SUPPORTED_SOCIAL_PLATFORMS or value != payload.platform:
        raise HTTPException(status_code=400, detail="publish platform mismatch")
    task = db.get(Task, payload.task_id, with_for_update=True)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    account = db.get(Account, payload.account_id, with_for_update=True)
    if not account or account.platform != Platform(value) or not account.is_active:
        raise HTTPException(status_code=400, detail="active account for platform not found")
    if account.check_state != "valid":
        raise HTTPException(status_code=400, detail="social account must be validated before publishing")
    try:
        meta = normalize_social_publish_meta(payload.meta, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    active_states = [PublishState.submitting, PublishState.submitted, PublishState.unknown, PublishState.published]
    existing = (
        db.query(PublishJob)
        .filter(
            PublishJob.task_id == payload.task_id,
            PublishJob.platform == Platform(value),
            PublishJob.account_id == payload.account_id,
            PublishJob.state.in_(active_states),
        )
        .order_by(PublishJob.created_at.desc())
        .first()
    )
    if existing and not (payload.force_retry and existing.state in {PublishState.submitted, PublishState.unknown}):
        return _response_from_job(existing)

    meta_json = {
        "platform": value,
        "account_id": str(account.id),
        "account_name": account.name,
        "video": payload.video.model_dump(),
        "cover": payload.cover.model_dump() if payload.cover else None,
        "meta": meta,
        "platform_options": payload.platform_options,
    }
    if existing:
        meta_json["retry_of_job_id"] = str(existing.id)
    job = PublishJob(
        task_id=task.id,
        platform=Platform(value),
        account_id=account.id,
        cover_key=payload.cover.key if payload.cover else None,
        meta_json=meta_json,
        state=PublishState.submitting,
    )
    db.add(job)
    if task.status not in {TaskStatus.published, TaskStatus.canceled}:
        task.status = TaskStatus.publishing
        task.error_code = None
        task.error_message = None
        db.add(task)
    db.commit()
    db.refresh(job)
    celery_app.send_task("social_publisher.process_job", args=[str(job.id)], queue="social_publish")
    return _response_from_job(job)
