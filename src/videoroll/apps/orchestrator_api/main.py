from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional
from urllib.parse import quote, urlparse

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from videoroll.config import OrchestratorSettings, get_orchestrator_settings
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, PublishJob, Subtitle, SubtitleJob, Task, TaskStatus
from videoroll.db.session import db_session, get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.utils.hashing import sha256_file
from videoroll.apps.orchestrator_api.schemas import (
    AssetRead,
    AutoYouTubeRequest,
    AutoYouTubeResponse,
    ConvertedVideoItem,
    PublishActionRequest,
    PublishJobSummary,
    RemoteJobResponse,
    RemotePublishResponse,
    StorageRetentionSettingsRead,
    StorageRetentionSettingsUpdate,
    SubtitleActionRequest,
    SubtitleJobSummary,
    TaskCreate,
    TaskRead,
    YouTubeProxyTestRequest,
    YouTubeProxyTestResponse,
    YouTubeSettingsRead,
    YouTubeSettingsUpdate,
    YouTubeDownloadActionResponse,
    YouTubeMetaActionResponse,
    YouTubeMetaRead,
)
from videoroll.apps.orchestrator_api.youtube_downloader import (
    download_youtube_video,
    download_thumbnail_jpg,
    extract_youtube_metadata,
    summarize_info,
)
from videoroll.apps.orchestrator_api.storage_retention_store import (
    get_storage_retention_settings,
    update_storage_retention_settings,
)
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_tags
from videoroll.apps.subtitle_service.task_title_store import get_task_display_title
from videoroll.apps.youtube_settings_store import get_youtube_settings, update_youtube_settings


def get_settings() -> OrchestratorSettings:
    return get_orchestrator_settings()


def get_db(settings: OrchestratorSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


def get_s3(settings: OrchestratorSettings = Depends(get_settings)) -> S3Store:
    store = S3Store(settings)
    store.ensure_bucket()
    return store


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _publish_job_error_message(job: PublishJob) -> str | None:
    data = _as_dict(job.response_json)
    if not data:
        return None

    msg = str(data.get("error") or data.get("message") or data.get("detail") or "").strip()
    if not msg:
        return None

    extras: list[str] = []
    for k in ["code", "status_code", "v_voucher"]:
        v = data.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            extras.append(f"{k}={s}")
    if extras:
        msg = f"{msg} ({', '.join(extras)})"

    if len(msg) > 500:
        msg = msg[:499] + "…"
    return msg


def _normalize_tags(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        parts = [p.strip() for p in v.replace("，", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(v, (list, tuple, set)):
        out: list[str] = []
        for item in v:
            s = str(item or "").strip()
            if s:
                out.append(s)
        return out
    s = str(v or "").strip()
    return [s] if s else []


def _clean_download_filename(name: str, *, max_len: int = 120) -> str:
    s = str(name or "").replace("\r", " ").replace("\n", " ").strip()
    s = s.replace("/", " ").replace("\\", " ")
    s = s.replace('"', "'")
    s = " ".join(s.split())
    if not s:
        return "download.bin"
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def _content_disposition(filename: str, *, inline: bool) -> str:
    disp = "inline" if inline else "attachment"
    fn = _clean_download_filename(filename)

    # ASCII fallback for legacy clients.
    fallback = "".join(c if 32 <= ord(c) < 127 else "_" for c in fn)
    fallback = fallback.replace("\\", "_").replace('"', "_").strip() or "download.bin"
    encoded = quote(fn, safe="")
    return f"{disp}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _suggest_asset_filename(db: Session, task_id: uuid.UUID, asset: Asset) -> str:
    base = Path(asset.storage_key).name or "download.bin"
    if asset.kind == AssetKind.video_final:
        title = get_task_display_title(db, str(task_id)).strip()
        if title:
            ext = Path(base).suffix
            if ext and len(ext) <= 8:
                return f"{title}{ext}"
            return title
    return base


def _parse_range_header(range_header: str, total_size: int) -> tuple[int, int] | None:
    """
    Supports a single range of the form:
      - bytes=start-end
      - bytes=start-
      - bytes=-suffix_len
    Returns (start, end) inclusive.
    """
    if total_size <= 0:
        return None
    raw = str(range_header or "").strip().lower()
    if not raw.startswith("bytes="):
        return None
    spec = raw[len("bytes=") :].split(",")[0].strip()
    if not spec:
        return None
    if spec.startswith("-"):
        try:
            suffix_len = int(spec[1:])
        except Exception:
            return None
        if suffix_len <= 0:
            return None
        end = total_size - 1
        start = max(0, total_size - suffix_len)
        return start, end

    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        start = int(start_s)
    except Exception:
        return None
    if start < 0:
        return None

    if end_s.strip() == "":
        end = total_size - 1
    else:
        try:
            end = int(end_s)
        except Exception:
            return None
    if end < start:
        return None
    if start >= total_size:
        return None
    end = min(end, total_size - 1)
    return start, end


def _youtube_meta_to_read(meta: Any) -> YouTubeMetaRead:
    return YouTubeMetaRead(
        title=str(getattr(meta, "title", "") or ""),
        description=str(getattr(meta, "description", "") or ""),
        webpage_url=str(getattr(meta, "webpage_url", "") or ""),
        uploader=getattr(meta, "uploader", None),
        upload_date=getattr(meta, "upload_date", None),
        duration=getattr(meta, "duration", None),
    )


def _read_s3_bytes(s3: S3Store, key: str) -> bytes:
    obj = s3.get_object(key)
    body = obj.get("Body")
    if not body:
        return b""
    try:
        return body.read() or b""
    finally:
        try:
            body.close()
        except Exception:
            pass


app = FastAPI(title="videoroll-orchestrator", version="0.1.0")

_cleanup_stop = threading.Event()
_cleanup_thread: Optional[threading.Thread] = None
_cleanup_interval_seconds = int(os.getenv("STORAGE_CLEANUP_INTERVAL_SECONDS", "3600") or "3600")


_cors_origins = [
    o.strip()
    for o in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    settings = get_orchestrator_settings()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    S3Store(settings).ensure_bucket()
    _start_cleanup_thread()


@app.on_event("shutdown")
def _shutdown() -> None:
    _cleanup_stop.set()
    global _cleanup_thread
    if _cleanup_thread and _cleanup_thread.is_alive():
        _cleanup_thread.join(timeout=2.0)
    _cleanup_thread = None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _cleanup_storage_once(settings: OrchestratorSettings) -> dict[str, int]:
    SessionLocal = get_sessionmaker(settings.database_url)
    db = SessionLocal()
    try:
        cfg = get_storage_retention_settings(db)
        ttl_days = int(cfg.get("asset_ttl_days") or 0)
        if ttl_days <= 0:
            return {"deleted_objects": 0, "deleted_assets": 0, "deleted_subtitles": 0}

        cutoff = _utcnow() - timedelta(days=ttl_days)
        store = S3Store(settings)
        store.ensure_bucket()

        deleted_objects = 0
        deleted_assets = 0
        deleted_subtitles = 0

        # 1) Delete fully-expired keys (no Asset rows >= cutoff).
        while True:
            rows = (
                db.query(Asset.storage_key)
                .group_by(Asset.storage_key)
                .having(func.max(Asset.created_at) < cutoff)
                .limit(200)
                .all()
            )
            keys = [r[0] for r in rows if r and r[0]]
            if not keys:
                break

            for key in keys:
                try:
                    store.delete_object(key)
                    deleted_objects += 1
                except Exception:
                    # Best-effort: still delete DB rows to honor TTL.
                    pass

            deleted_assets += (
                db.query(Asset)
                .filter(Asset.storage_key.in_(keys))
                .delete(synchronize_session=False)
            )
            deleted_subtitles += (
                db.query(Subtitle)
                .filter(Subtitle.storage_key.in_(keys))
                .delete(synchronize_session=False)
            )
            db.commit()

        # 2) Delete old duplicate rows for keys that still exist (object kept).
        deleted_assets += db.query(Asset).filter(Asset.created_at < cutoff).delete(synchronize_session=False)
        deleted_subtitles += db.query(Subtitle).filter(Subtitle.created_at < cutoff).delete(synchronize_session=False)
        db.commit()

        return {
            "deleted_objects": deleted_objects,
            "deleted_assets": deleted_assets,
            "deleted_subtitles": deleted_subtitles,
        }
    finally:
        db.close()


def _cleanup_loop() -> None:
    settings = get_orchestrator_settings()
    while not _cleanup_stop.is_set():
        try:
            _cleanup_storage_once(settings)
        except Exception:
            pass
        _cleanup_stop.wait(timeout=max(30, _cleanup_interval_seconds))


def _start_cleanup_thread() -> None:
    global _cleanup_thread
    if _cleanup_thread and _cleanup_thread.is_alive():
        return
    _cleanup_stop.clear()
    t = threading.Thread(target=_cleanup_loop, name="videoroll-storage-cleanup", daemon=True)
    t.start()
    _cleanup_thread = t


def _effective_youtube_settings(settings: OrchestratorSettings, db: Session) -> OrchestratorSettings:
    cfg = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    proxy = str(cfg.get("proxy") or "").strip()
    return settings.model_copy(update={"youtube_proxy": proxy or None})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auto/youtube", response_model=AutoYouTubeResponse)
def auto_youtube(
    payload: AutoYouTubeRequest,
    settings: OrchestratorSettings = Depends(get_settings),
) -> AutoYouTubeResponse:
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if not _is_youtube_url(url):
        raise HTTPException(status_code=400, detail="url is not a valid youtube url")

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{settings.youtube_ingest_url}/youtube/ingest",
                json={"url": url, "license": payload.license.value, "proof_url": payload.proof_url},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"youtube-ingest: {e.response.text}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"youtube-ingest request failed: {e}") from e

    try:
        task_id = uuid.UUID(str(data.get("task_id")))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"youtube-ingest returned invalid task_id: {data}") from e

    from videoroll.apps.subtitle_service.worker import celery_app as subtitle_celery_app

    res = subtitle_celery_app.send_task("subtitle_service.auto_youtube_pipeline", args=[str(task_id)], queue="subtitle")
    return AutoYouTubeResponse(
        task_id=task_id,
        pipeline_job_id=str(res.id),
        deduped=bool(data.get("deduped")),
        source_id=str(data.get("source_id") or "") or None,
    )


@app.post("/tasks", response_model=TaskRead)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)) -> Task:
    task = Task(
        source_type=payload.source_type,
        source_url=payload.source_url,
        source_license=payload.source_license,
        source_proof_url=payload.source_proof_url,
        priority=payload.priority,
        created_by=payload.created_by,
        status=TaskStatus.created,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@app.get("/videos/converted", response_model=list[ConvertedVideoItem])
def list_converted_videos(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Fetch recent video_final assets and dedupe by task_id (keep latest per task).
    fetch_n = min(1000, max(limit, 1) * 5)
    assets = (
        db.query(Asset)
        .filter(Asset.kind == AssetKind.video_final)
        .order_by(Asset.created_at.desc())
        .limit(fetch_n)
        .all()
    )

    items: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for a in assets:
        if a.task_id in seen:
            continue
        seen.add(a.task_id)
        task = db.get(Task, a.task_id)
        if not task:
            continue
        cover_asset = (
            db.query(Asset)
            .filter(Asset.task_id == a.task_id, Asset.kind == AssetKind.cover_image)
            .order_by(Asset.created_at.desc())
            .first()
        )
        display_title = get_task_display_title(db, str(a.task_id))
        items.append({"task": task, "final_asset": a, "cover_asset": cover_asset, "display_title": display_title or None})
        if len(items) >= limit:
            break
    return items


@app.get("/tasks", response_model=list[TaskRead])
def list_tasks(
    status: Optional[TaskStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Task]:
    q = db.query(Task).order_by(Task.created_at.desc())
    if status is not None:
        q = q.filter(Task.status == status)
    return q.limit(limit).all()


@app.get("/settings/storage", response_model=StorageRetentionSettingsRead)
def get_storage_settings(db: Session = Depends(get_db)) -> StorageRetentionSettingsRead:
    cfg = get_storage_retention_settings(db)
    return StorageRetentionSettingsRead(**cfg)


@app.put("/settings/storage", response_model=StorageRetentionSettingsRead)
def put_storage_settings(payload: StorageRetentionSettingsUpdate, db: Session = Depends(get_db)) -> StorageRetentionSettingsRead:
    try:
        cfg = update_storage_retention_settings(db, payload.model_dump(exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return StorageRetentionSettingsRead(**cfg)


@app.get("/settings/youtube", response_model=YouTubeSettingsRead)
def get_youtube_settings_view(settings: OrchestratorSettings = Depends(get_settings), db: Session = Depends(get_db)) -> YouTubeSettingsRead:
    cfg = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    return YouTubeSettingsRead(**cfg)


@app.put("/settings/youtube", response_model=YouTubeSettingsRead)
def put_youtube_settings_view(
    payload: YouTubeSettingsUpdate,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> YouTubeSettingsRead:
    try:
        cfg = update_youtube_settings(db, payload.model_dump(exclude_unset=True), default_proxy=settings.youtube_proxy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return YouTubeSettingsRead(**cfg)


@app.post("/settings/youtube/test", response_model=YouTubeProxyTestResponse)
def test_youtube_proxy(
    payload: YouTubeProxyTestRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> YouTubeProxyTestResponse:
    url = str(payload.url or "").strip() or "https://www.youtube.com/robots.txt"

    if payload.proxy is not None:
        proxy = str(payload.proxy or "").strip()
    else:
        cfg = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
        proxy = str(cfg.get("proxy") or "").strip()

    start = time.perf_counter()
    headers = {"User-Agent": settings.youtube_user_agent}
    client_kwargs: dict[str, Any] = {"timeout": 20.0, "follow_redirects": True, "headers": headers}
    if proxy:
        try:
            client_kwargs["proxy"] = proxy
        except Exception:
            pass

    try:
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.get(url)
                ok = resp.status_code < 400
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return YouTubeProxyTestResponse(
                    ok=ok,
                    url=url,
                    used_proxy=proxy or None,
                    status_code=resp.status_code,
                    elapsed_ms=elapsed_ms,
                )
        except TypeError:
            with httpx.Client(timeout=20.0, follow_redirects=True, headers=headers) as client:
                resp = client.get(url)
                ok = resp.status_code < 400
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return YouTubeProxyTestResponse(
                    ok=ok,
                    url=url,
                    used_proxy=None,
                    status_code=resp.status_code,
                    elapsed_ms=elapsed_ms,
                    error="httpx does not support proxy kwarg in this environment",
                )
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return YouTubeProxyTestResponse(
            ok=False,
            url=url,
            used_proxy=proxy or None,
            status_code=None,
            elapsed_ms=elapsed_ms,
            error=str(e),
        )


@app.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.post("/tasks/{task_id}/upload/video", response_model=AssetRead)
async def upload_task_video(
    task_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> Asset:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    suffix = Path(file.filename or "").suffix or ".mp4"
    key = f"raw/{task_id}/video{suffix}"

    with tempfile.NamedTemporaryFile(prefix="videoroll_", suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)

    sha256 = sha256_file(tmp_path)
    size_bytes = tmp_path.stat().st_size
    s3.upload_file(tmp_path, key)
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass

    asset = Asset(
        task_id=task_id,
        kind=AssetKind.video_raw,
        storage_key=key,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    db.add(asset)
    task.status = TaskStatus.downloaded
    db.add(task)
    db.commit()
    db.refresh(asset)
    return asset


@app.post("/tasks/{task_id}/upload/cover", response_model=AssetRead)
async def upload_task_cover(
    task_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> Asset:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="cover must be an image")

    suffix = Path(file.filename or "").suffix or ".jpg"
    key = f"final/{task_id}/cover{suffix}"

    with tempfile.NamedTemporaryFile(prefix="videoroll_cover_", suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)

    sha256 = sha256_file(tmp_path)
    size_bytes = tmp_path.stat().st_size
    s3.upload_file(tmp_path, key)
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass

    asset = Asset(
        task_id=task_id,
        kind=AssetKind.cover_image,
        storage_key=key,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@app.get("/tasks/{task_id}/assets", response_model=list[AssetRead])
def list_task_assets(task_id: uuid.UUID, db: Session = Depends(get_db)) -> list[Asset]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return db.query(Asset).filter(Asset.task_id == task_id).order_by(Asset.created_at.asc()).all()


@app.get("/tasks/{task_id}/assets/{asset_id}/download")
def download_task_asset(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> StreamingResponse:
    asset = db.get(Asset, asset_id)
    if not asset or asset.task_id != task_id:
        raise HTTPException(status_code=404, detail="asset not found")

    resp = s3.get_object(asset.storage_key)
    body = resp["Body"]
    media_type = resp.get("ContentType") or "application/octet-stream"
    filename = _suggest_asset_filename(db, task_id, asset)

    headers = {"Content-Disposition": _content_disposition(filename, inline=False)}
    length = resp.get("ContentLength") or asset.size_bytes
    if isinstance(length, int):
        headers["Content-Length"] = str(length)

    return StreamingResponse(S3Store.iter_body(body), media_type=media_type, headers=headers)


@app.get("/tasks/{task_id}/assets/{asset_id}/stream")
def stream_task_asset(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> StreamingResponse:
    asset = db.get(Asset, asset_id)
    if not asset or asset.task_id != task_id:
        raise HTTPException(status_code=404, detail="asset not found")

    filename = _suggest_asset_filename(db, task_id, asset)

    total_size: int | None = None
    media_type = "application/octet-stream"
    try:
        head = s3.head_object(asset.storage_key)
        if isinstance(head.get("ContentLength"), int):
            total_size = int(head["ContentLength"])
        if head.get("ContentType"):
            media_type = str(head["ContentType"]) or media_type
    except Exception:
        total_size = asset.size_bytes if isinstance(asset.size_bytes, int) else None

    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(filename, inline=True),
    }

    range_header = request.headers.get("range") or ""
    if range_header and isinstance(total_size, int) and total_size > 0:
        parsed = _parse_range_header(range_header, total_size)
        if not parsed:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total_size}"})
        start, end = parsed

        resp = s3.get_object(asset.storage_key, range_bytes=f"bytes={start}-{end}")
        body = resp["Body"]
        headers = {
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{total_size}",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(
            S3Store.iter_body(body),
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    resp = s3.get_object(asset.storage_key)
    body = resp["Body"]
    media_type = resp.get("ContentType") or media_type
    headers = dict(base_headers)
    length = resp.get("ContentLength") or asset.size_bytes
    if isinstance(length, int):
        headers["Content-Length"] = str(length)
    return StreamingResponse(S3Store.iter_body(body), media_type=media_type, headers=headers)


@app.delete("/tasks/{task_id}/assets/{asset_id}")
def delete_task_asset(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> dict[str, bool]:
    asset = db.get(Asset, asset_id)
    if not asset or asset.task_id != task_id:
        raise HTTPException(status_code=404, detail="asset not found")

    if asset.kind != AssetKind.video_final:
        raise HTTPException(status_code=400, detail="only video_final assets can be deleted")

    s3.delete_object(asset.storage_key)
    db.query(Subtitle).filter(Subtitle.task_id == task_id, Subtitle.storage_key == asset.storage_key).delete(
        synchronize_session=False
    )
    db.delete(asset)
    db.commit()
    return {"deleted": True}


def _is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return host.endswith("youtube.com") or host == "youtu.be" or host.endswith(".youtube.com")


@app.post("/tasks/{task_id}/actions/youtube_meta", response_model=YouTubeMetaActionResponse)
def fetch_youtube_meta(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> YouTubeMetaActionResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_type.value != "youtube":
        raise HTTPException(status_code=400, detail="task is not a youtube source")

    url = (task.source_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="task.source_url is empty")
    if not _is_youtube_url(url):
        raise HTTPException(status_code=400, detail="task.source_url is not a valid youtube url")

    latest = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if latest:
        try:
            raw = _read_s3_bytes(s3, latest.storage_key)
            info_cached = json.loads(raw.decode("utf-8")) if raw else {}
            meta = summarize_info(_as_dict(info_cached), fallback_url=url)
            return YouTubeMetaActionResponse(metadata=_youtube_meta_to_read(meta), metadata_asset=latest)
        except Exception:
            # Fall back to re-fetch via yt-dlp.
            pass

    try:
        yt_settings = _effective_youtube_settings(settings, db)
        info, meta = extract_youtube_metadata(url, yt_settings)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"youtube metadata failed: {e}") from e
    key = f"raw/{task_id}/metadata.json"
    payload = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_bytes(payload, key, content_type="application/json")

    asset = Asset(
        task_id=task_id,
        kind=AssetKind.metadata_json,
        storage_key=key,
        sha256=_sha256_bytes(payload),
        size_bytes=len(payload),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return YouTubeMetaActionResponse(metadata=_youtube_meta_to_read(meta), metadata_asset=asset)


@app.post("/tasks/{task_id}/actions/youtube_download", response_model=YouTubeDownloadActionResponse)
def download_youtube(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> YouTubeDownloadActionResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_type.value != "youtube":
        raise HTTPException(status_code=400, detail="task is not a youtube source")

    url = (task.source_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="task.source_url is empty")
    if not _is_youtube_url(url):
        raise HTTPException(status_code=400, detail="task.source_url is not a valid youtube url")

    existing_video = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_raw)
        .order_by(Asset.created_at.desc())
        .first()
    )

    yt_settings = _effective_youtube_settings(settings, db)

    work_root = Path(yt_settings.work_dir) / "youtube" / str(task_id)
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ytdlp_", dir=str(work_root)) as tmp:
        tmp_dir = Path(tmp)
        video_asset = existing_video
        info: dict[str, Any]
        meta: Any

        if video_asset is None:
            try:
                video_path, info, meta = download_youtube_video(url, yt_settings, work_dir=tmp_dir)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"youtube download failed: {e}") from e

            suffix = video_path.suffix.lower() or ".mp4"
            video_key = f"raw/{task_id}/video{suffix}"
            s3.upload_file(video_path, video_key)
            video_asset = Asset(
                task_id=task_id,
                kind=AssetKind.video_raw,
                storage_key=video_key,
                sha256=sha256_file(video_path),
                size_bytes=video_path.stat().st_size,
            )
            db.add(video_asset)
        else:
            try:
                info, meta = extract_youtube_metadata(url, yt_settings)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"youtube metadata failed: {e}") from e

        meta_key = f"raw/{task_id}/metadata.json"
        meta_payload = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
        s3.put_bytes(meta_payload, meta_key, content_type="application/json")
        meta_asset = Asset(
            task_id=task_id,
            kind=AssetKind.metadata_json,
            storage_key=meta_key,
            sha256=_sha256_bytes(meta_payload),
            size_bytes=len(meta_payload),
        )
        db.add(meta_asset)

        cover_asset: Optional[Asset] = None
        try:
            cover_path = download_thumbnail_jpg(_as_dict(info), yt_settings, work_dir=tmp_dir)
            if cover_path:
                cover_sha = sha256_file(cover_path)
                existing_cover = (
                    db.query(Asset)
                    .filter(Asset.task_id == task_id, Asset.kind == AssetKind.cover_image, Asset.sha256 == cover_sha)
                    .order_by(Asset.created_at.desc())
                    .first()
                )
                if existing_cover:
                    cover_asset = existing_cover
                else:
                    cover_key = f"final/{task_id}/cover_youtube_{cover_sha[:12]}.jpg"
                    s3.upload_file(cover_path, cover_key, content_type="image/jpeg")
                    cover_asset = Asset(
                        task_id=task_id,
                        kind=AssetKind.cover_image,
                        storage_key=cover_key,
                        sha256=cover_sha,
                        size_bytes=cover_path.stat().st_size,
                    )
                    db.add(cover_asset)
        except Exception:
            cover_asset = None

        if task.status in {TaskStatus.created, TaskStatus.ingested}:
            task.status = TaskStatus.downloaded
            db.add(task)

        db.commit()
        db.refresh(meta_asset)
        if video_asset is not None:
            db.refresh(video_asset)
        if cover_asset is not None:
            try:
                db.refresh(cover_asset)
            except Exception:
                pass

    return YouTubeDownloadActionResponse(
        metadata=_youtube_meta_to_read(meta),
        metadata_asset=meta_asset,
        video_asset=video_asset,
        cover_asset=cover_asset,
    )


@app.get("/tasks/{task_id}/subtitle_jobs", response_model=list[SubtitleJobSummary])
def list_task_subtitle_jobs(task_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=500), db: Session = Depends(get_db)) -> list[SubtitleJob]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return db.query(SubtitleJob).filter(SubtitleJob.task_id == task_id).order_by(SubtitleJob.created_at.desc()).limit(limit).all()


@app.get("/tasks/{task_id}/publish_jobs", response_model=list[PublishJobSummary])
def list_task_publish_jobs(task_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    jobs = db.query(PublishJob).filter(PublishJob.task_id == task_id).order_by(PublishJob.created_at.desc()).limit(limit).all()
    out: list[dict[str, Any]] = []
    for j in jobs:
        out.append(
            {
                "id": j.id,
                "task_id": j.task_id,
                "state": j.state.value,
                "aid": j.aid,
                "bvid": j.bvid,
                "error_message": _publish_job_error_message(j),
                "created_at": j.created_at,
                "updated_at": j.updated_at,
            }
        )
    return out


@app.post("/tasks/{task_id}/actions/subtitle", response_model=RemoteJobResponse)
def enqueue_subtitle_job(
    task_id: uuid.UUID,
    payload: SubtitleActionRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    raw_asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_raw)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not raw_asset:
        msg = "no raw video asset found; upload video first"
        if task.source_type.value == "youtube" and (task.source_url or "").strip():
            msg = f"{msg} (youtube task: POST /tasks/{task_id}/actions/youtube_download)"
        raise HTTPException(status_code=400, detail=msg)

    translate: dict[str, Any] = {
        "enabled": payload.translate_enabled,
        "target_lang": payload.target_lang,
        "provider": payload.translate_provider,
        "style": payload.translate_style,
        "glossary_id": None,
        "bilingual": payload.bilingual,
    }
    if payload.translate_batch_size is not None:
        translate["batch_size"] = payload.translate_batch_size
    if payload.translate_enable_summary is not None:
        translate["enable_summary"] = payload.translate_enable_summary

    req = {
        "task_id": str(task_id),
        "input": {"type": "s3", "key": raw_asset.storage_key},
        "asr": {"engine": payload.asr_engine, "language": payload.asr_language, "model": payload.asr_model},
        "translate": translate,
        "output": {
            "formats": payload.formats,
            "render": {
                "burn_in": payload.burn_in,
                "soft_sub": payload.soft_sub,
                "ass_style": payload.ass_style,
                "video_codec": payload.video_codec,
            },
        },
        "output_prefix": f"sub/{task_id}/",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{settings.subtitle_service_url}/subtitle/jobs", json=req)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"subtitle-service request failed: {e}") from e

    return RemoteJobResponse(job_id=uuid.UUID(data["job_id"]), status=str(data.get("status", "queued")))


@app.post("/tasks/{task_id}/actions/publish", response_model=RemotePublishResponse)
def enqueue_publish_job(
    task_id: uuid.UUID,
    payload: PublishActionRequest,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemotePublishResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_license.value == "unknown":
        raise HTTPException(status_code=400, detail="source_license=unknown; add proof before publishing")

    video_key = payload.video_key
    if not video_key:
        final_asset = (
            db.query(Asset)
            .filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_final)
            .order_by(Asset.created_at.desc())
            .first()
        )
        if not final_asset:
            raise HTTPException(status_code=400, detail="no final video asset found; render first")
        video_key = final_asset.storage_key

    meta = dict(payload.meta or {})
    try:
        copyright_val = int(meta.get("copyright") or 1)
    except Exception:
        copyright_val = 1
    if copyright_val == 2 and not str(meta.get("source") or "").strip() and task.source_url:
        meta["source"] = task.source_url

    # Auto-append generated Bilibili tags (from subtitle translation stage).
    existing_tags = _normalize_tags(meta.get("tags"))
    auto_tags = get_task_bilibili_tags(db, str(task_id))
    merged_tags: list[str] = []
    seen: set[str] = set()
    for t in ["videoroll", *auto_tags, *existing_tags]:
        s = str(t or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        merged_tags.append(s)
        if len(merged_tags) >= 10:
            break
    if merged_tags:
        meta["tags"] = merged_tags

    req = {
        "task_id": str(task_id),
        "account_id": payload.account_id,
        "video": {"type": "s3", "key": video_key},
        "cover": {"type": "s3", "key": payload.cover_key} if payload.cover_key else None,
        "meta": meta,
    }
    typeid_mode = str(payload.typeid_mode or "").strip()
    if typeid_mode:
        req["typeid_mode"] = typeid_mode

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{settings.bilibili_publisher_url}/bilibili/publish", json=req)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        detail: str
        try:
            body = e.response.json()
            detail = str(body.get("detail") or body.get("message") or body)
        except Exception:
            detail = e.response.text
        raise HTTPException(status_code=e.response.status_code, detail=f"bilibili-publisher: {detail}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"bilibili-publisher request failed: {e}") from e

    return RemotePublishResponse(**data)


@app.exception_handler(ValueError)
def value_error_handler(_req, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})
