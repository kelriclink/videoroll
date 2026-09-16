from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_s3, get_settings
from videoroll.apps.orchestrator_api.schemas import (
    LiveAudioControlRequest,
    LiveAudioPlaylistCreate,
    LiveAudioPlaylistRead,
    LiveDashboardRead,
    LiveInputSourceCreate,
    LiveInputSourceRead,
    LiveInputSourceUpdate,
    LiveMediaImportRequest,
    LiveMediaRead,
    LiveMediaRenameRequest,
    LiveManualPlayRequest,
    LivePlaylistRead,
    LivePlaylistUpdate,
    LiveSessionRead,
    LiveStreamSettingsRead,
    LiveStreamSettingsUpdate,
)
from videoroll.apps.orchestrator_api.services import live_service
from videoroll.config import OrchestratorSettings
from videoroll.storage.filesystem import FileStore


router = APIRouter()


def _legacy_live_mutation_guard(
    settings: OrchestratorSettings = Depends(get_settings),
) -> None:
    if not settings.legacy_live_enabled:
        raise HTTPException(
            status_code=410,
            detail="旧直播系统已停用，请使用播控中心",
        )


async def _upload_live_media_batch(
    media_type: str,
    files: list[UploadFile],
    *,
    db: Session,
    s3: FileStore,
    audio_playlist_id: uuid.UUID | None = None,
) -> list[LiveMediaRead]:
    uploaded: list[LiveMediaRead] = []
    for index, file in enumerate(files, start=1):
        try:
            uploaded.append(
                LiveMediaRead(
                    **await live_service.upload_live_media(
                        media_type,
                        file,
                        db=db,
                        s3=s3,
                        audio_playlist_id=audio_playlist_id,
                    )
                )
            )
        except HTTPException as exc:
            name = str(file.filename or "未命名文件")
            completed = f"；此前 {len(uploaded)} 个文件已保留在媒体库" if uploaded else ""
            raise HTTPException(
                status_code=exc.status_code,
                detail=f"第 {index}/{len(files)} 个文件“{name}”上传失败：{exc.detail}{completed}",
            ) from exc
    return uploaded


@router.get("/live", response_model=LiveDashboardRead)
def get_live_dashboard(db: Session = Depends(get_db)) -> LiveDashboardRead:
    return LiveDashboardRead(**live_service.get_live_dashboard(db))


@router.get("/live/legacy-status")
def get_legacy_live_status(
    settings: OrchestratorSettings = Depends(get_settings),
) -> dict[str, bool]:
    return {"enabled": settings.legacy_live_enabled}


@router.get("/live/settings", response_model=LiveStreamSettingsRead)
def get_live_settings(db: Session = Depends(get_db)) -> LiveStreamSettingsRead:
    return LiveStreamSettingsRead(**live_service.get_live_settings(db))


@router.put("/live/settings", response_model=LiveStreamSettingsRead)
def put_live_settings(
    payload: LiveStreamSettingsUpdate,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveStreamSettingsRead:
    return LiveStreamSettingsRead(**live_service.update_live_settings(db, payload.model_dump(exclude_unset=True)))


@router.put("/live/playlist", response_model=LivePlaylistRead)
def put_live_playlist(
    payload: LivePlaylistUpdate,
    db: Session = Depends(get_db),
    s3: FileStore = Depends(get_s3),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LivePlaylistRead:
    return LivePlaylistRead(
        **live_service.update_live_playlist(
            db,
            payload.model_dump(exclude_unset=True, mode="json"),
            s3=s3,
        )
    )


@router.post("/live/media/video", response_model=list[LiveMediaRead])
async def upload_live_video(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    s3: FileStore = Depends(get_s3),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> list[LiveMediaRead]:
    return await _upload_live_media_batch("video", files, db=db, s3=s3)


@router.post("/live/media/audio", response_model=list[LiveMediaRead])
async def upload_live_audio(
    files: list[UploadFile] = File(...),
    audio_playlist_id: uuid.UUID | None = Form(default=None),
    db: Session = Depends(get_db),
    s3: FileStore = Depends(get_s3),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> list[LiveMediaRead]:
    return await _upload_live_media_batch(
        "audio",
        files,
        db=db,
        s3=s3,
        audio_playlist_id=audio_playlist_id,
    )


@router.post("/live/media/import", response_model=list[LiveMediaRead])
def import_live_task_videos(
    payload: LiveMediaImportRequest,
    db: Session = Depends(get_db),
    s3: FileStore = Depends(get_s3),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> list[LiveMediaRead]:
    return [
        LiveMediaRead(**media)
        for media in live_service.import_task_videos(payload.asset_ids, db=db, s3=s3)
    ]


@router.get("/live/media/{media_id}/stream")
def stream_live_media(
    media_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    s3: FileStore = Depends(get_s3),
) -> Response:
    result = live_service.prepare_live_media_stream(
        db,
        s3,
        media_id,
        range_header=request.headers.get("range") or "",
    )
    if result.body is None:
        return Response(status_code=result.status_code, headers=result.headers)
    return StreamingResponse(
        FileStore.iter_body(result.body),
        status_code=result.status_code,
        media_type=result.media_type,
        headers=result.headers,
    )


@router.get("/live/preview/{file_name}")
def stream_live_preview(
    file_name: str,
    settings: OrchestratorSettings = Depends(get_settings),
) -> FileResponse:
    path = live_service.live_preview_path(settings, file_name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="当前没有可用的直播预览")
    media_type = "application/vnd.apple.mpegurl" if path.suffix == ".m3u8" else "video/mp2t"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.delete("/live/media/{media_id}")
def delete_live_media(
    media_id: uuid.UUID,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> dict[str, bool]:
    return live_service.delete_live_media(media_id, db=db)


@router.patch("/live/media/{media_id}", response_model=LiveMediaRead)
def rename_live_video_media(
    media_id: uuid.UUID,
    payload: LiveMediaRenameRequest,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveMediaRead:
    return LiveMediaRead(**live_service.rename_live_video_media(media_id, payload.display_name, db=db))


@router.post("/live/audio-playlists", response_model=LiveAudioPlaylistRead)
def create_live_audio_playlist(
    payload: LiveAudioPlaylistCreate,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveAudioPlaylistRead:
    return LiveAudioPlaylistRead(**live_service.create_live_audio_playlist(db, payload.model_dump(mode="json")))


@router.delete("/live/audio-playlists/{playlist_id}")
def delete_live_audio_playlist(
    playlist_id: uuid.UUID,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> dict[str, bool]:
    return live_service.delete_live_audio_playlist(playlist_id, db=db)


@router.post("/live/sources", response_model=LiveInputSourceRead)
def create_live_source(
    payload: LiveInputSourceCreate,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveInputSourceRead:
    return LiveInputSourceRead(**live_service.create_live_input_source(db, payload.model_dump(mode="json")))


@router.put("/live/sources/{source_id}", response_model=LiveInputSourceRead)
def update_live_source(
    source_id: uuid.UUID,
    payload: LiveInputSourceUpdate,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveInputSourceRead:
    return LiveInputSourceRead(
        **live_service.update_live_input_source(
            source_id,
            payload.model_dump(exclude_unset=True, mode="json"),
            db=db,
        )
    )


@router.delete("/live/sources/{source_id}")
def delete_live_source(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> dict[str, bool]:
    return live_service.delete_live_input_source(source_id, db=db)


@router.post("/live/actions/start", response_model=LiveSessionRead)
def start_live_stream(
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveSessionRead:
    return LiveSessionRead(**live_service.start_live_stream(settings, db=db))


@router.post("/live/actions/play", response_model=LiveSessionRead)
def play_live_selection(
    payload: LiveManualPlayRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveSessionRead:
    return LiveSessionRead(
        **live_service.play_live_selection(
            settings,
            payload.model_dump(mode="json"),
            db=db,
        )
    )


@router.post("/live/actions/audio", response_model=LiveSessionRead)
def control_live_audio(
    payload: LiveAudioControlRequest,
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveSessionRead:
    return LiveSessionRead(**live_service.control_live_audio(payload.model_dump(mode="json"), db=db))


@router.post("/live/actions/pause", response_model=LiveSessionRead)
def pause_live_stream(
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveSessionRead:
    return LiveSessionRead(**live_service.pause_live_stream(db=db))


@router.post("/live/actions/resume", response_model=LiveSessionRead)
def resume_live_stream(
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveSessionRead:
    return LiveSessionRead(**live_service.resume_live_stream(db=db))


@router.post("/live/actions/stop", response_model=LiveSessionRead)
def stop_live_stream(
    db: Session = Depends(get_db),
    _legacy_guard: None = Depends(_legacy_live_mutation_guard),
) -> LiveSessionRead:
    return LiveSessionRead(**live_service.stop_live_stream(db=db))
