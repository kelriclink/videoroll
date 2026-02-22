from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Generator

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from videoroll.config import BilibiliPublisherSettings, get_bilibili_publisher_settings
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, PublishJob, PublishState, Task, TaskStatus
from videoroll.db.session import db_session, get_engine
from videoroll.storage.s3 import S3Store
from videoroll.apps.bilibili_publisher.auth_settings_store import get_bilibili_auth_settings, get_bilibili_cookie_header, update_bilibili_auth_settings
from videoroll.apps.bilibili_publisher.publish_settings_store import get_bilibili_publish_settings, update_bilibili_publish_settings
from videoroll.apps.bilibili_publisher.worker import celery_app
from videoroll.apps.bilibili_publisher.schemas import (
    BilibiliAuthSettingsRead,
    BilibiliAuthSettingsUpdate,
    BilibiliMeRead,
    BilibiliPublishSettingsRead,
    BilibiliPublishSettingsUpdate,
    PublishJobRead,
    PublishRequest,
    PublishResponse,
)


def get_settings() -> BilibiliPublisherSettings:
    return get_bilibili_publisher_settings()


def get_db(settings: BilibiliPublisherSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


app = FastAPI(title="videoroll-bilibili-publisher", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
        if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    settings = get_bilibili_publisher_settings()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    S3Store(settings).ensure_bucket()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/bilibili/publish/settings", response_model=BilibiliPublishSettingsRead)
def get_publish_settings(db: Session = Depends(get_db)) -> BilibiliPublishSettingsRead:
    cfg = get_bilibili_publish_settings(db)
    return BilibiliPublishSettingsRead(default_meta=cfg["default_meta"])


@app.put("/bilibili/publish/settings", response_model=BilibiliPublishSettingsRead)
def put_publish_settings(payload: BilibiliPublishSettingsUpdate, db: Session = Depends(get_db)) -> BilibiliPublishSettingsRead:
    try:
        cfg = update_bilibili_publish_settings(db, payload.model_dump(exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return BilibiliPublishSettingsRead(default_meta=cfg["default_meta"])


@app.get("/bilibili/auth/settings", response_model=BilibiliAuthSettingsRead)
def get_auth_settings(db: Session = Depends(get_db)) -> BilibiliAuthSettingsRead:
    cfg = get_bilibili_auth_settings(db)
    return BilibiliAuthSettingsRead(**cfg)


@app.put("/bilibili/auth/settings", response_model=BilibiliAuthSettingsRead)
def put_auth_settings(payload: BilibiliAuthSettingsUpdate, db: Session = Depends(get_db)) -> BilibiliAuthSettingsRead:
    try:
        cfg = update_bilibili_auth_settings(db, payload.model_dump(exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return BilibiliAuthSettingsRead(**cfg)


@app.get("/bilibili/auth/me", response_model=BilibiliMeRead)
def get_auth_me(db: Session = Depends(get_db)) -> BilibiliMeRead:
    cookie = get_bilibili_cookie_header(db).strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="bilibili cookie is not set")

    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": "videoroll/0.1", "Cookie": cookie}) as client:
            resp = client.get("https://api.bilibili.com/x/member/web/account")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"bilibili request failed: {e}") from e

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="bilibili returned unexpected response")
    code = data.get("code")
    if code != 0:
        raise HTTPException(status_code=401, detail=f"not logged in (code={code} message={data.get('message')})")
    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    try:
        mid = int(d.get("mid") or 0)
    except Exception:
        mid = 0
    uname = str(d.get("uname") or "").strip()
    if mid <= 0 or not uname:
        raise HTTPException(status_code=502, detail="bilibili returned empty user info")
    return BilibiliMeRead(
        mid=mid,
        uname=uname,
        userid=str(d.get("userid") or "") or None,
        sign=str(d.get("sign") or "") or None,
        rank=str(d.get("rank") or "") or None,
    )


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@app.post("/bilibili/publish", response_model=PublishResponse)
def publish(payload: PublishRequest, settings: BilibiliPublisherSettings = Depends(get_settings), db: Session = Depends(get_db)) -> PublishResponse:
    task = db.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    job = PublishJob(
        task_id=payload.task_id,
        meta_json={
            "meta": payload.meta.model_dump(),
            "video": payload.video.model_dump(),
            "account_id": payload.account_id,
        },
        cover_key=payload.cover.key if payload.cover else None,
        state=PublishState.submitting,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    if settings.publish_mode != "mock":
        task.status = TaskStatus.publishing
        db.add(task)
        db.commit()
        celery_app.send_task("bilibili_publisher.process_job", args=[str(job.id)], queue="publish")
        return PublishResponse(state=job.state.value)

    aid = str(int(uuid.uuid4().int % 1_000_000_000))
    bvid = "BV" + uuid.uuid4().hex[:10]
    response = {"mode": "mock", "aid": aid, "bvid": bvid}

    job.state = PublishState.published
    job.aid = aid
    job.bvid = bvid
    job.response_json = response
    db.add(job)

    task.status = TaskStatus.published
    db.add(task)

    store = S3Store(settings)
    store.ensure_bucket()
    result_key = f"meta/{task.id}/publish_result.json"
    store.put_bytes(json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8"), result_key, content_type="application/json")
    db.add(Asset(task_id=task.id, kind=AssetKind.publish_result, storage_key=result_key))

    db.commit()
    return PublishResponse(state=job.state.value, aid=aid, bvid=bvid, response=response)


@app.get("/bilibili/publish/jobs/{job_id}", response_model=PublishJobRead)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> PublishJob:
    job = db.get(PublishJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job
