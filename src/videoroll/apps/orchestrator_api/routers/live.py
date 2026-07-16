from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_s3, get_settings
from videoroll.apps.orchestrator_api.schemas import (
    LiveDashboardRead,
    LiveMediaRead,
    LivePlaylistRead,
    LivePlaylistUpdate,
    LiveSessionRead,
    LiveStreamSettingsRead,
    LiveStreamSettingsUpdate,
)
from videoroll.apps.orchestrator_api.services import live_service
from videoroll.config import OrchestratorSettings
from videoroll.storage.s3 import S3Store


router = APIRouter()


@router.get("/live", response_model=LiveDashboardRead)
def get_live_dashboard(db: Session = Depends(get_db)) -> LiveDashboardRead:
    return LiveDashboardRead(**live_service.get_live_dashboard(db))


@router.get("/live/settings", response_model=LiveStreamSettingsRead)
def get_live_settings(db: Session = Depends(get_db)) -> LiveStreamSettingsRead:
    return LiveStreamSettingsRead(**live_service.get_live_settings(db))


@router.put("/live/settings", response_model=LiveStreamSettingsRead)
def put_live_settings(
    payload: LiveStreamSettingsUpdate,
    db: Session = Depends(get_db),
) -> LiveStreamSettingsRead:
    return LiveStreamSettingsRead(**live_service.update_live_settings(db, payload.model_dump(exclude_unset=True)))


@router.put("/live/playlist", response_model=LivePlaylistRead)
def put_live_playlist(
    payload: LivePlaylistUpdate,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> LivePlaylistRead:
    return LivePlaylistRead(
        **live_service.update_live_playlist(
            db,
            payload.model_dump(exclude_unset=True, mode="json"),
            s3=s3,
        )
    )


@router.post("/live/media/video", response_model=LiveMediaRead)
async def upload_live_video(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> LiveMediaRead:
    return LiveMediaRead(**await live_service.upload_live_media("video", file, db=db, s3=s3))


@router.post("/live/media/audio", response_model=LiveMediaRead)
async def upload_live_audio(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> LiveMediaRead:
    return LiveMediaRead(**await live_service.upload_live_media("audio", file, db=db, s3=s3))


@router.get("/live/media/{media_id}/stream")
def stream_live_media(
    media_id: uuid.UUID,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> StreamingResponse:
    body, content_type, headers = live_service.prepare_live_media_stream(db, s3, media_id)
    return StreamingResponse(S3Store.iter_body(body), media_type=content_type, headers=headers)


@router.delete("/live/media/{media_id}")
def delete_live_media(media_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, bool]:
    return live_service.delete_live_media(media_id, db=db)


@router.post("/live/actions/start", response_model=LiveSessionRead)
def start_live_stream(
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> LiveSessionRead:
    return LiveSessionRead(**live_service.start_live_stream(settings, db=db))


@router.post("/live/actions/pause", response_model=LiveSessionRead)
def pause_live_stream(db: Session = Depends(get_db)) -> LiveSessionRead:
    return LiveSessionRead(**live_service.pause_live_stream(db=db))


@router.post("/live/actions/resume", response_model=LiveSessionRead)
def resume_live_stream(db: Session = Depends(get_db)) -> LiveSessionRead:
    return LiveSessionRead(**live_service.resume_live_stream(db=db))


@router.post("/live/actions/stop", response_model=LiveSessionRead)
def stop_live_stream(db: Session = Depends(get_db)) -> LiveSessionRead:
    return LiveSessionRead(**live_service.stop_live_stream(db=db))
