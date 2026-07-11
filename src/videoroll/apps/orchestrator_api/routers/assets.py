from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_s3
from videoroll.apps.orchestrator_api.schemas import AssetRead
from videoroll.apps.orchestrator_api.services import asset_service
from videoroll.db.models import Asset
from videoroll.storage.s3 import S3Store


router = APIRouter()


def _stream_response(result: asset_service.AssetStreamResult) -> Response:
    if result.body is None:
        return Response(status_code=result.status_code, headers=result.headers)
    return StreamingResponse(
        S3Store.iter_body(result.body),
        status_code=result.status_code,
        media_type=result.media_type,
        headers=result.headers,
    )


@router.post("/tasks/{task_id}/upload/video", response_model=AssetRead)
async def upload_task_video(
    task_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> Asset:
    return await asset_service.upload_task_video(task_id, file, db=db, s3=s3)


@router.post("/tasks/{task_id}/upload/cover", response_model=AssetRead)
async def upload_task_cover(
    task_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> Asset:
    return await asset_service.upload_task_cover(task_id, file, db=db, s3=s3)


@router.get("/tasks/{task_id}/assets", response_model=list[AssetRead])
def list_task_assets(task_id: uuid.UUID, db: Session = Depends(get_db)) -> list[Asset]:
    return asset_service.list_task_assets(db, task_id)


@router.get("/tasks/{task_id}/assets/{asset_id}/download")
def download_task_asset(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> Response:
    return _stream_response(
        asset_service.prepare_asset_download(db, s3, task_id=task_id, asset_id=asset_id)
    )


@router.get("/tasks/{task_id}/assets/{asset_id}/stream")
def stream_task_asset(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> Response:
    return _stream_response(
        asset_service.prepare_asset_stream(
            db,
            s3,
            task_id=task_id,
            asset_id=asset_id,
            range_header=request.headers.get("range") or "",
        )
    )


@router.delete("/tasks/{task_id}/assets/{asset_id}")
def delete_task_asset(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> dict[str, bool]:
    return asset_service.delete_final_asset(task_id=task_id, asset_id=asset_id, db=db, s3=s3)
