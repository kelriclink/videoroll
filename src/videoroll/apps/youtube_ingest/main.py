from __future__ import annotations

import os
import uuid
from typing import Generator, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from videoroll.config import YouTubeIngestSettings, get_youtube_ingest_settings
from videoroll.db.base import Base
from videoroll.db.models import (
    IngestedVideo,
    SourceType,
    Task,
    TaskStatus,
    YouTubeSource,
)
from videoroll.db.session import db_session, get_engine
from videoroll.storage.s3 import S3Store
from videoroll.apps.youtube_ingest.schemas import (
    YouTubeIngestRequest,
    YouTubeIngestResponse,
    YouTubeScanRequest,
    YouTubeScanResponse,
    YouTubeSourceCreate,
    YouTubeSourceRead,
)
from videoroll.apps.youtube_ingest.youtube_feed import fetch_youtube_feed
from videoroll.apps.youtube_settings_store import get_youtube_settings


def get_settings() -> YouTubeIngestSettings:
    return get_youtube_ingest_settings()


def get_db(settings: YouTubeIngestSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


app = FastAPI(title="videoroll-youtube-ingest", version="0.1.0")

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
    settings = get_youtube_ingest_settings()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    S3Store(settings).ensure_bucket()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/youtube/sources", response_model=YouTubeSourceRead)
def create_source(payload: YouTubeSourceCreate, db: Session = Depends(get_db)) -> YouTubeSource:
    src = YouTubeSource(
        source_type=payload.source_type,
        source_id=payload.source_id,
        license=payload.license,
        proof_url=payload.proof_url,
        enabled=payload.enabled,
    )
    db.add(src)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"create source failed: {e}") from e
    db.refresh(src)
    return src


@app.get("/youtube/sources", response_model=list[YouTubeSourceRead])
def list_sources(db: Session = Depends(get_db)) -> list[YouTubeSource]:
    return db.query(YouTubeSource).order_by(YouTubeSource.created_at.desc()).all()


def _extract_video_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.netloc in {"youtu.be"}:
        vid = parsed.path.strip("/").split("/")[0]
        return vid or None
    if "youtube.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        vid = (qs.get("v") or [None])[0]
        return vid or None
    return None


@app.post("/youtube/ingest", response_model=YouTubeIngestResponse)
def ingest_single(payload: YouTubeIngestRequest, db: Session = Depends(get_db)) -> YouTubeIngestResponse:
    video_id = _extract_video_id(payload.url)
    if video_id:
        existing = db.query(IngestedVideo).filter(IngestedVideo.platform == "youtube", IngestedVideo.source_id == video_id).first()
        if existing:
            return YouTubeIngestResponse(task_id=existing.task_id, deduped=True, source_id=video_id)

    task = Task(
        source_type=SourceType.youtube,
        source_url=payload.url,
        source_license=payload.license,
        source_proof_url=payload.proof_url,
        status=TaskStatus.ingested,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    if video_id:
        db.add(IngestedVideo(platform="youtube", source_id=video_id, task_id=task.id))
        db.commit()

    return YouTubeIngestResponse(task_id=task.id, deduped=False, source_id=video_id)


@app.post("/youtube/scan", response_model=YouTubeScanResponse)
def scan_source(
    payload: YouTubeScanRequest,
    settings: YouTubeIngestSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> YouTubeScanResponse:
    src = db.query(YouTubeSource).filter(YouTubeSource.source_type == payload.source_type, YouTubeSource.source_id == payload.source_id).first()
    if not src or not src.enabled:
        raise HTTPException(status_code=400, detail="source not found or disabled")

    yt_cfg = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    try:
        entries = list(
            fetch_youtube_feed(
                src.source_type.value,
                src.source_id,
                user_agent=settings.user_agent,
                proxy=str(yt_cfg.get("proxy") or "").strip() or None,
                limit=payload.limit,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"fetch youtube feed failed: {e}") from e
    if payload.since:
        entries = [e for e in entries if e.published_at > payload.since]
    entries = entries[: payload.limit]

    created: list[uuid.UUID] = []
    started: list[str] = []
    skipped = 0
    for e in entries:
        exists = db.query(IngestedVideo).filter(IngestedVideo.platform == "youtube", IngestedVideo.source_id == e.video_id).first()
        if exists:
            skipped += 1
            continue

        url = f"https://www.youtube.com/watch?v={e.video_id}"
        task = Task(
            source_type=SourceType.youtube,
            source_url=url,
            source_license=src.license,
            source_proof_url=src.proof_url,
            status=TaskStatus.ingested,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        db.add(IngestedVideo(platform="youtube", source_id=e.video_id, task_id=task.id, published_at=e.published_at))
        db.commit()
        created.append(task.id)

        if payload.auto_process:
            try:
                from videoroll.apps.subtitle_service.worker import celery_app as subtitle_celery_app

                res = subtitle_celery_app.send_task(
                    "subtitle_service.auto_youtube_pipeline",
                    args=[str(task.id)],
                    queue="subtitle",
                )
                started.append(str(res.id))
            except Exception:
                pass

    return YouTubeScanResponse(
        discovered_count=len(entries),
        created_task_ids=created,
        skipped_duplicates=skipped,
        started_pipeline_job_ids=started,
    )
