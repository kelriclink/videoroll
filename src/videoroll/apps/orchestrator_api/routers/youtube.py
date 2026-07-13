from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.dependencies import get_db, get_s3, get_settings
from videoroll.apps.orchestrator_api.remote_api_settings_store import (
    REMOTE_API_TOKEN_QUERY_PARAM,
    REMOTE_AUTO_YOUTUBE_PATH,
    remote_api_token_is_configured,
    verify_remote_api_token,
)
from videoroll.apps.orchestrator_api.schemas import (
    AutoYouTubeRequest,
    AutoYouTubeResponse,
    AutoYouTubeTaskStartResponse,
    YouTubeDownloadActionResponse,
    YouTubeHomeScanRunResponse,
    YouTubeMetaActionResponse,
    YouTubeMetaRead,
    YouTubeProxyTestRequest,
    YouTubeProxyTestResponse,
)
from videoroll.apps.orchestrator_api.services import youtube_service
from videoroll.apps.youtube_settings_store import get_youtube_settings
from videoroll.config import OrchestratorSettings
from videoroll.db.models import SourceLicense
from videoroll.storage.s3 import S3Store


router = APIRouter()


@router.api_route("/youtube/{service_path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def proxy_youtube_browser_operation(
    service_path: str,
    request: Request,
    settings: OrchestratorSettings = Depends(get_settings),
) -> Response:
    try:
        response = await youtube_service.proxy_browser_request(
            settings,
            service_path=f"youtube/{service_path}",
            method=request.method,
            query_string=request.url.query,
            body=await request.body(),
            content_type=request.headers.get("content-type"),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"youtube-ingest request failed: {exc}") from exc
    return Response(content=response.content, status_code=response.status_code, headers=response.headers)


@router.post("/auto/youtube", response_model=AutoYouTubeResponse)
def auto_youtube(payload: AutoYouTubeRequest, settings: OrchestratorSettings = Depends(get_settings)) -> AutoYouTubeResponse:
    return youtube_service.start_auto_youtube_pipeline(url=payload.url, license=payload.license, proof_url=payload.proof_url, auto_publish=payload.auto_publish, settings=settings)


@router.get(REMOTE_AUTO_YOUTUBE_PATH, response_model=AutoYouTubeResponse)
@router.post(REMOTE_AUTO_YOUTUBE_PATH, response_model=AutoYouTubeResponse)
def remote_auto_youtube(request: Request, url: str | None = Query(default=None), token: str | None = Query(default=None, alias=REMOTE_API_TOKEN_QUERY_PARAM), license: SourceLicense = Query(default=SourceLicense.authorized), proof_url: str | None = Query(default=None), auto_publish: bool | None = Query(default=None), settings: OrchestratorSettings = Depends(get_settings), db: Session = Depends(get_db)) -> AutoYouTubeResponse:
    if not remote_api_token_is_configured(db):
        raise HTTPException(status_code=403, detail="remote api token is not set")
    auth = str(request.headers.get("authorization") or "").strip()
    effective_token = (auth[7:].strip() if auth.lower().startswith("bearer ") else "") or str(token or "")
    if not verify_remote_api_token(db, effective_token):
        raise HTTPException(status_code=401, detail="invalid remote api token")
    return youtube_service.start_auto_youtube_pipeline(url=str(url or ""), license=license, proof_url=proof_url, auto_publish=auto_publish, settings=settings)


@router.post("/tasks/{task_id}/actions/auto_youtube_start", response_model=AutoYouTubeTaskStartResponse)
def start_auto_youtube_for_existing_task(task_id: uuid.UUID, settings: OrchestratorSettings = Depends(get_settings), db: Session = Depends(get_db)) -> AutoYouTubeTaskStartResponse:
    return youtube_service.start_existing_task(task_id, settings=settings, db=db)


@router.post("/settings/youtube/home_scan/run", response_model=YouTubeHomeScanRunResponse)
def run_youtube_home_scan_now(settings: OrchestratorSettings = Depends(get_settings)) -> YouTubeHomeScanRunResponse:
    try:
        result = youtube_service.run_home_scan(settings, force=True, raise_if_locked=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=409 if "already running" in str(exc).lower() else 400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"youtube home scan request failed: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"youtube home scan failed: {type(exc).__name__}: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=409, detail="youtube home scan did not run")
    return result


@router.post("/settings/youtube/test", response_model=YouTubeProxyTestResponse)
def test_youtube_proxy(payload: YouTubeProxyTestRequest, settings: OrchestratorSettings = Depends(get_settings), db: Session = Depends(get_db)) -> YouTubeProxyTestResponse:
    url = str(payload.url or "").strip() or "https://www.youtube.com/robots.txt"
    proxy = str(payload.proxy or "").strip() if payload.proxy is not None else str(get_youtube_settings(db, default_proxy=settings.youtube_proxy).get("proxy") or "").strip()
    return youtube_service.test_proxy(url=url, proxy=proxy, settings=settings)


@router.get("/tasks/{task_id}/youtube_meta", response_model=YouTubeMetaRead)
def get_cached_youtube_meta(task_id: uuid.UUID, db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)) -> YouTubeMetaRead:
    return youtube_service.get_cached_meta(task_id, db=db, s3=s3)


@router.post("/tasks/{task_id}/actions/youtube_meta", response_model=YouTubeMetaActionResponse)
def fetch_youtube_meta(task_id: uuid.UUID, settings: OrchestratorSettings = Depends(get_settings), db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)) -> YouTubeMetaActionResponse:
    return youtube_service.fetch_meta(task_id, settings=settings, db=db, s3=s3)


@router.post("/tasks/{task_id}/actions/youtube_download", response_model=YouTubeDownloadActionResponse)
def download_youtube(task_id: uuid.UUID, settings: OrchestratorSettings = Depends(get_settings), db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)) -> YouTubeDownloadActionResponse:
    return youtube_service.download(task_id, settings=settings, db=db, s3=s3)
