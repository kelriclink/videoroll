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

from videoroll.config import BilibiliPublisherSettings, get_bilibili_publisher_settings, get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, PublishJob, PublishState, Task, TaskStatus
from videoroll.db.session import db_session, get_engine
from videoroll.storage.s3 import S3Store
from videoroll.apps.bilibili_publisher.auth_settings_store import get_bilibili_auth_settings, get_bilibili_cookie_header, update_bilibili_auth_settings
from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient
from videoroll.apps.bilibili_publisher.typeid_recommender import flatten_typelist, recommend_typeid_openai
from videoroll.apps.bilibili_publisher.publish_settings_store import get_bilibili_publish_settings, update_bilibili_publish_settings
from videoroll.apps.bilibili_publisher.worker import celery_app
from videoroll.apps.bilibili_publisher.schemas import (
    BilibiliAuthSettingsRead,
    BilibiliAuthSettingsUpdate,
    BilibiliArchiveTypesRead,
    BilibiliTypeRecommendRequest,
    BilibiliTypeRecommendResponse,
    BilibiliMeRead,
    BilibiliPublishSettingsRead,
    BilibiliPublishSettingsUpdate,
    PublishJobRead,
    PublishRequest,
    PublishResponse,
)
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_summary
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings


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


@app.get("/bilibili/archive/types", response_model=BilibiliArchiveTypesRead)
def get_archive_types(db: Session = Depends(get_db)) -> BilibiliArchiveTypesRead:
    cookie = get_bilibili_cookie_header(db).strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="bilibili cookie is not set")

    try:
        with BilibiliWebClient(cookie) as client:
            pre = client.archive_pre()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"bilibili request failed: {e}") from e

    data = pre.get("data") if isinstance(pre, dict) else {}
    data = data if isinstance(data, dict) else {}
    typelist = data.get("typelist") if isinstance(data.get("typelist"), list) else []
    return BilibiliArchiveTypesRead(typelist=typelist)


@app.post("/bilibili/archive/type/recommend", response_model=BilibiliTypeRecommendResponse)
def recommend_archive_type(
    payload: BilibiliTypeRecommendRequest,
    settings: BilibiliPublisherSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> BilibiliTypeRecommendResponse:
    cookie = get_bilibili_cookie_header(db).strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="bilibili cookie is not set")
    translate_settings = get_translate_settings(db, get_subtitle_settings())
    if not translate_settings.get("openai_api_key"):
        raise HTTPException(status_code=400, detail="OpenAI API key is not set (save it in Settings · Translate)")

    text = get_task_bilibili_summary(db, str(payload.task_id)) if payload.task_id else ""
    if not text:
        text = str(payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="missing text (and no task summary found)")
    if len(text) > 2000:
        text = text[:1999] + "…"

    try:
        with BilibiliWebClient(cookie) as client:
            pre = client.archive_pre()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"bilibili request failed: {e}") from e

    data = pre.get("data") if isinstance(pre, dict) else {}
    data = data if isinstance(data, dict) else {}
    typelist = data.get("typelist") if isinstance(data.get("typelist"), list) else []
    options = flatten_typelist(typelist)
    if not options:
        raise HTTPException(status_code=502, detail="bilibili typelist is empty")

    by_id = {int(o.get("id") or 0): str(o.get("path") or "").strip() for o in options}
    by_id = {k: v for k, v in by_id.items() if k > 0 and v}

    try:
        obj = recommend_typeid_openai(
            text,
            options=options,
            api_key=str(translate_settings.get("openai_api_key") or ""),
            base_url=str(translate_settings.get("openai_base_url") or ""),
            model=str(translate_settings.get("openai_model") or ""),
            temperature=float(translate_settings.get("openai_temperature") or 0.0),
            timeout_seconds=float(translate_settings.get("openai_timeout_seconds") or 30.0),
        )
        try:
            tid = int(obj.get("typeid") or 0)
        except Exception:
            tid = 0
        reason = str(obj.get("reason") or "").strip()
    except Exception as e:
        return BilibiliTypeRecommendResponse(ok=False, reason=str(e), used_text=text)

    if tid <= 0 or tid not in by_id:
        return BilibiliTypeRecommendResponse(ok=False, reason="typeid not in candidate list", used_text=text)

    return BilibiliTypeRecommendResponse(ok=True, typeid=tid, path=by_id.get(tid), reason=reason, used_text=text)


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
            "typeid_mode": payload.typeid_mode,
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
