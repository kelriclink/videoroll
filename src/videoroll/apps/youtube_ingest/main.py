from __future__ import annotations

import os
import uuid
from typing import Generator

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.config import YouTubeIngestSettings, get_youtube_ingest_settings
from videoroll.db.base import Base
from videoroll.db.auto_migrate import auto_migrate
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
    YouTubeSourceScanRequest,
    YouTubeSourceUpdate,
)
from videoroll.apps.youtube_ingest.source_service import (
    scan_youtube_source_by_id,
    source_to_read_dict,
    update_youtube_source,
    upsert_youtube_source,
)
from videoroll.utils.youtube_urls import canonicalize_youtube_url, extract_youtube_video_id


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
    auto_migrate(settings.database_url)
    S3Store(settings).ensure_bucket()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/youtube/sources", response_model=YouTubeSourceRead)
def create_source(
    payload: YouTubeSourceCreate,
    settings: YouTubeIngestSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> YouTubeSourceRead:
    try:
        src = upsert_youtube_source(
            db,
            source_input=payload.source_url,
            source_type=payload.source_type,
            source_id=payload.source_id,
            license=payload.license,
            proof_url=payload.proof_url,
            enabled=payload.enabled,
            scan_interval_minutes=payload.scan_interval_minutes,
            scan_limit=payload.scan_limit,
            auto_process=payload.auto_process,
            user_agent=settings.user_agent,
            default_proxy=settings.youtube_proxy,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return YouTubeSourceRead(**source_to_read_dict(src))


@app.get("/youtube/sources", response_model=list[YouTubeSourceRead])
def list_sources(db: Session = Depends(get_db)) -> list[YouTubeSourceRead]:
    rows = db.query(YouTubeSource).order_by(YouTubeSource.created_at.desc()).all()
    return [YouTubeSourceRead(**source_to_read_dict(row)) for row in rows]


@app.patch("/youtube/sources/{source_pk}", response_model=YouTubeSourceRead)
def patch_source(
    source_pk: uuid.UUID,
    payload: YouTubeSourceUpdate,
    db: Session = Depends(get_db),
) -> YouTubeSourceRead:
    try:
        src = update_youtube_source(db, source_pk, payload.model_dump(exclude_unset=True))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return YouTubeSourceRead(**source_to_read_dict(src))


@app.delete("/youtube/sources/{source_pk}")
def delete_source(source_pk: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, bool]:
    src = db.get(YouTubeSource, source_pk)
    if src is None:
        raise HTTPException(status_code=404, detail="source not found")
    db.delete(src)
    db.commit()
    return {"ok": True}


@app.post("/youtube/ingest", response_model=YouTubeIngestResponse)
def ingest_single(payload: YouTubeIngestRequest, db: Session = Depends(get_db)) -> YouTubeIngestResponse:
    normalized_url = canonicalize_youtube_url(payload.url)
    video_id = extract_youtube_video_id(normalized_url)
    if video_id:
        existing = db.query(IngestedVideo).filter(IngestedVideo.platform == "youtube", IngestedVideo.source_id == video_id).first()
        if existing:
            return YouTubeIngestResponse(task_id=existing.task_id, deduped=True, source_id=video_id)

    task = Task(
        source_type=SourceType.youtube,
        source_url=normalized_url,
        source_license=payload.license,
        source_proof_url=payload.proof_url,
        status=TaskStatus.ingested,
    )
    db.add(task)
    if video_id:
        db.flush()
        db.add(IngestedVideo(platform="youtube", source_id=video_id, task_id=task.id))
    try:
        db.commit()
    except IntegrityError as e:
        # Concurrency-safe dedupe: another request inserted the same IngestedVideo.
        db.rollback()
        if video_id:
            existing = (
                db.query(IngestedVideo)
                .filter(IngestedVideo.platform == "youtube", IngestedVideo.source_id == video_id)
                .first()
            )
            if existing:
                return YouTubeIngestResponse(task_id=existing.task_id, deduped=True, source_id=video_id)
        raise HTTPException(status_code=500, detail=f"ingest failed: {e}") from e

    db.refresh(task)
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
    try:
        res = scan_youtube_source_by_id(
            db,
            src.id,
            user_agent=settings.user_agent,
            default_proxy=settings.youtube_proxy,
            limit_override=payload.limit,
            auto_process_override=payload.auto_process,
            since=payload.since,
            force=True,
            raise_if_locked=True,
            lock_owner_prefix="manual_youtube_source_scan",
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        msg = str(e)
        if "already running" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=502, detail=msg) from e

    return YouTubeScanResponse(
        discovered_count=res.discovered_count,
        created_task_ids=res.created_task_ids,
        skipped_duplicates=res.skipped_duplicates,
        started_pipeline_job_ids=res.started_pipeline_job_ids,
    )


@app.post("/youtube/sources/{source_pk}/scan", response_model=YouTubeScanResponse)
def scan_source_by_row_id(
    source_pk: uuid.UUID,
    payload: YouTubeSourceScanRequest = Body(default=YouTubeSourceScanRequest()),
    settings: YouTubeIngestSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> YouTubeScanResponse:
    try:
        res = scan_youtube_source_by_id(
            db,
            source_pk,
            user_agent=settings.user_agent,
            default_proxy=settings.youtube_proxy,
            limit_override=payload.limit,
            auto_process_override=payload.auto_process,
            force=True,
            raise_if_locked=True,
            lock_owner_prefix="manual_youtube_source_scan",
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        msg = str(e)
        if "already running" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=502, detail=msg) from e

    return YouTubeScanResponse(
        discovered_count=res.discovered_count,
        created_task_ids=res.created_task_ids,
        skipped_duplicates=res.skipped_duplicates,
        started_pipeline_job_ids=res.started_pipeline_job_ids,
    )
