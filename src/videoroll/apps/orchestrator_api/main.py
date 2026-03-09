from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional
from urllib.parse import quote

import httpx
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from sqlalchemy import func
from sqlalchemy.orm import Session

from videoroll.config import OrchestratorSettings, get_orchestrator_settings
from videoroll.db.base import Base
from videoroll.db.auto_migrate import auto_migrate
from videoroll.db.models import AppSetting, Asset, AssetKind, PublishJob, SourceLicense, Subtitle, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.db.session import db_session, get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.utils.internal_api_token import internal_api_token
from videoroll.utils.hashing import sha256_file
from videoroll.utils.youtube_urls import canonicalize_youtube_url, is_youtube_url
from videoroll.apps.orchestrator_api.schemas import (
    AdminAuthLoginRequest,
    AdminAuthSetupRequest,
    AdminAuthStatusRead,
    AssetRead,
    AutoYouTubeRequest,
    AutoYouTubeResponse,
    ConvertedVideoItem,
    PublishActionRequest,
    PublishJobSummary,
    RemoteAPISettingsRead,
    RemoteAPISettingsUpdate,
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
from videoroll.apps.orchestrator_api.admin_auth_store import (
    DEVICE_COOKIE_NAME,
    INTERNAL_TOKEN_HEADER,
    device_cookie_max_age_seconds,
    encode_password_hash,
    get_password_hash,
    set_password_hash,
    mint_device_cookie_value,
    validate_new_password,
    verify_device_cookie_value,
    verify_password_hash,
)
from videoroll.apps.orchestrator_api.youtube_downloader import (
    YtDlpRuntimeError,
    download_youtube_video,
    download_thumbnail_jpg,
    extract_youtube_metadata,
    summarize_info,
)
from videoroll.apps.orchestrator_api.storage_retention_store import (
    get_storage_retention_settings,
    update_storage_retention_settings,
)
from videoroll.apps.orchestrator_api.remote_api_settings_store import (
    REMOTE_API_TOKEN_QUERY_PARAM,
    REMOTE_AUTO_YOUTUBE_PATH,
    get_remote_api_settings,
    remote_api_token_is_configured,
    update_remote_api_settings,
    verify_remote_api_token,
)
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_tags
from videoroll.apps.subtitle_service.task_title_store import get_task_display_title_with_s3
from videoroll.apps.youtube_settings_store import (
    get_youtube_cookies_txt,
    get_youtube_settings,
    normalize_and_validate_netscape_cookies_txt,
    summarize_netscape_cookies_txt,
    update_youtube_settings,
)

logger = logging.getLogger(__name__)


def get_settings() -> OrchestratorSettings:
    return get_orchestrator_settings()


def get_db(settings: OrchestratorSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


def get_s3(settings: OrchestratorSettings = Depends(get_settings)) -> S3Store:
    return S3Store(settings)


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


def _suggest_asset_filename(db: Session, task_id: uuid.UUID, asset: Asset, *, s3: S3Store | None) -> str:
    base = Path(asset.storage_key).name or "download.bin"
    if asset.kind == AssetKind.video_final:
        title = get_task_display_title_with_s3(db, str(task_id), s3=s3).strip()
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


def _publish_meta_s3_key(task_id: uuid.UUID) -> str:
    return f"meta/{task_id}/publish_meta.json"


def _read_s3_json_object(s3: S3Store, key: str) -> dict[str, Any] | None:
    try:
        raw = _read_s3_bytes(s3, key)
    except ClientError as e:
        code = str((_as_dict(e.response.get("Error")).get("Code") or "")).strip()
        if code in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_s3_object_missing(e: ClientError) -> bool:
    code = str((_as_dict(e.response.get("Error")).get("Code") or "")).strip()
    return code in {"NoSuchKey", "404", "NotFound"}


def _write_s3_json(s3: S3Store, key: str, obj: dict[str, Any]) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_bytes(payload, key, content_type="application/json")
    return payload


def _write_s3_text(s3: S3Store, key: str, text: str) -> bytes:
    payload = str(text).encode("utf-8")
    s3.put_bytes(payload, key, content_type="text/plain; charset=utf-8")
    return payload


def _store_task_log_asset(
    db: Session,
    s3: S3Store,
    *,
    task_id: uuid.UUID,
    log_key: str,
    text: str,
) -> Asset:
    payload = _write_s3_text(s3, log_key, text)
    asset = Asset(
        task_id=task_id,
        kind=AssetKind.log,
        storage_key=log_key,
        sha256=_sha256_bytes(payload),
        size_bytes=len(payload),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def _build_youtube_download_failure_log(
    *,
    task_id: uuid.UUID,
    url: str,
    error_message: str,
    hint: str | None,
    diagnostics: list[str] | None,
) -> str:
    body = "\n".join([str(line or "") for line in (diagnostics or [])]).strip()
    sections = [
        body or "videoroll yt-dlp diagnostics unavailable",
        "\n".join(
            [
                "---- videoroll error summary ----",
                f"task_id={task_id}",
                f"url={url}",
                f"error={error_message}",
            ]
        ),
    ]
    hint_text = str(hint or "").strip()
    if hint_text:
        sections.append(f"---- videoroll hint ----\n{hint_text}")
    return "\n\n".join(sections).rstrip() + "\n"


def _youtube_download_log_key(task_id: uuid.UUID) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"log/{task_id}/youtube_download_{stamp}_{uuid.uuid4().hex[:8]}.log"


def _extract_metadata_title(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return ""
    info = parsed if isinstance(parsed, dict) else {}
    return str(info.get("title") or info.get("fulltitle") or info.get("alt_title") or "").strip()


def _task_title_key(task_id: uuid.UUID) -> str:
    return f"task.title.{task_id}"


def _load_task_display_titles(
    db: Session,
    task_ids: list[uuid.UUID],
    *,
    s3: S3Store | None = None,
    allow_s3_fallback: bool,
) -> dict[uuid.UUID, str]:
    title_map: dict[uuid.UUID, str] = {}
    if not task_ids:
        return title_map

    rows = db.query(AppSetting).filter(AppSetting.key.in_([_task_title_key(tid) for tid in task_ids])).all()
    by_key = {str(r.key): _as_dict(getattr(r, "value_json", None)) for r in rows}
    for tid in task_ids:
        data = by_key.get(_task_title_key(tid)) or {}
        for key in ("translated_title", "source_title"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                title_map[tid] = value.strip()
                break

    if not allow_s3_fallback or s3 is None:
        return title_map

    missing = [tid for tid in task_ids if tid not in title_map]
    if not missing:
        return title_map

    assets = (
        db.query(Asset)
        .filter(Asset.task_id.in_(missing), Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .all()
    )
    picked: dict[uuid.UUID, Asset] = {}
    for asset in assets:
        if asset.task_id not in picked:
            picked[asset.task_id] = asset
    for tid, asset in picked.items():
        try:
            title = _extract_metadata_title(_read_s3_bytes(s3, asset.storage_key))
        except Exception:
            continue
        if title:
            title_map[tid] = title
    return title_map


def _stream_upload_to_tempfile(
    file_obj: Any,
    *,
    prefix: str,
    suffix: str,
) -> tuple[Path, str, int]:
    tmp_path: Path | None = None
    sha256 = hashlib.sha256()
    size_bytes = 0
    try:
        with tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise TypeError("uploaded file stream returned non-bytes content")
                sha256.update(chunk)
                size_bytes += len(chunk)
                tmp.write(chunk)
        return tmp_path, sha256.hexdigest(), size_bytes
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def _safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


async def _store_uploaded_task_asset(
    *,
    task: Task,
    file: UploadFile,
    s3: S3Store,
    db: Session,
    temp_prefix: str,
    default_suffix: str,
    key_prefix: str,
    object_name_prefix: str,
    asset_kind: AssetKind,
    update_task_status: TaskStatus | None = None,
) -> Asset:
    suffix = Path(file.filename or "").suffix or default_suffix
    tmp_path: Path | None = None
    uploaded_key: str | None = None

    try:
        await file.seek(0)
        tmp_path, sha256, size_bytes = await run_in_threadpool(
            _stream_upload_to_tempfile,
            file.file,
            prefix=temp_prefix,
            suffix=suffix,
        )
        uploaded_key = f"{key_prefix}/{task.id}/{object_name_prefix}_{sha256[:16]}{suffix}"
        await run_in_threadpool(s3.upload_file, tmp_path, uploaded_key, file.content_type or None)

        asset = Asset(
            task_id=task.id,
            kind=asset_kind,
            storage_key=uploaded_key,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        db.add(asset)
        if update_task_status is not None:
            task.status = update_task_status
            db.add(task)
        db.commit()
        db.refresh(asset)
        return asset
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        if uploaded_key:
            try:
                await run_in_threadpool(s3.delete_object, uploaded_key)
            except Exception:
                logger.exception("failed to roll back uploaded S3 object", extra={"storage_key": uploaded_key})
        raise HTTPException(status_code=500, detail=f"upload failed: {e}") from e
    finally:
        await run_in_threadpool(_safe_unlink, tmp_path)
        try:
            await file.close()
        except Exception:
            pass


def _internal_header_token(settings: OrchestratorSettings) -> str:
    return internal_api_token(settings.s3_secret_access_key)


def _admin_cookie_secret(settings: OrchestratorSettings) -> str:
    # Separate derivation so leaking the internal header token does not automatically
    # reveal the cookie signing secret.
    return internal_api_token(settings.s3_secret_access_key + ":admin-cookie")


def _internal_http_headers(settings: OrchestratorSettings) -> dict[str, str]:
    tok = _internal_header_token(settings)
    return {INTERNAL_TOKEN_HEADER: tok} if tok else {}


class _AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        # Normalize path for allowlist checks.
        #
        # When running behind a reverse proxy or with `uvicorn --root-path`,
        # `request.url.path` may include `scope["root_path"]` (e.g. "/api/auth/status"),
        # while route matching is done on the "app path" (e.g. "/auth/status").
        scope_path = str(request.scope.get("path") or getattr(request.url, "path", "") or "/")
        root_path = str(request.scope.get("root_path") or "").strip()
        path = scope_path
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :] or "/"
        if path == "/health" or path.endswith("/health"):
            return await call_next(request)
        if path.startswith("/auth"):
            return await call_next(request)
        if path.rstrip("/") == REMOTE_AUTO_YOUTUBE_PATH:
            return await call_next(request)

        pw_hash = _get_admin_password_hash(request)
        if not pw_hash:
            return JSONResponse(status_code=403, content={"detail": "admin password not set"})

        internal_header_token = str(getattr(request.app.state, "internal_header_token", "") or "").strip()
        header_tok = str(request.headers.get(INTERNAL_TOKEN_HEADER) or "").strip()
        if internal_header_token and header_tok and hmac.compare_digest(header_tok, internal_header_token):
            return await call_next(request)

        cookie_val = str(request.cookies.get(DEVICE_COOKIE_NAME) or "").strip()
        cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
        if cookie_val and cookie_secret and verify_device_cookie_value(
            cookie_val,
            internal_secret=cookie_secret,
            password_hash=pw_hash,
        ):
            return await call_next(request)

        return JSONResponse(status_code=401, content={"detail": "unauthorized"})


app = FastAPI(title="videoroll-orchestrator", version="0.1.0")

_cleanup_stop = threading.Event()
_cleanup_thread: Optional[threading.Thread] = None
_cleanup_interval_seconds = int(os.getenv("STORAGE_CLEANUP_INTERVAL_SECONDS", "3600") or "3600")

app.add_middleware(_AdminAuthMiddleware)


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
    auto_migrate(settings.database_url)
    S3Store(settings).ensure_bucket()
    app.state.database_url = settings.database_url
    app.state.internal_header_token = _internal_header_token(settings)
    app.state.admin_cookie_secret = _admin_cookie_secret(settings)
    SessionLocal = get_sessionmaker(settings.database_url)
    db = SessionLocal()
    try:
        app.state.admin_password_hash = get_password_hash(db)
    finally:
        db.close()
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

        deleted_objects = 0
        deleted_assets = 0
        deleted_subtitles = 0
        failed_keys: set[str] = set()

        # 1) Delete fully-expired keys (no Asset rows >= cutoff).
        while True:
            query = db.query(Asset.storage_key).group_by(Asset.storage_key).having(func.max(Asset.created_at) < cutoff)
            if failed_keys:
                query = query.filter(~Asset.storage_key.in_(failed_keys))
            rows = query.limit(200).all()
            keys = [r[0] for r in rows if r and r[0]]
            if not keys:
                break

            deleted_keys: list[str] = []
            for key in keys:
                try:
                    store.delete_object(key)
                    deleted_objects += 1
                    deleted_keys.append(key)
                except Exception:
                    failed_keys.add(key)
                    logger.exception("failed to delete expired S3 object", extra={"storage_key": key})

            if not deleted_keys:
                continue

            deleted_assets += db.query(Asset).filter(Asset.storage_key.in_(deleted_keys)).delete(synchronize_session=False)
            deleted_subtitles += db.query(Subtitle).filter(Subtitle.storage_key.in_(deleted_keys)).delete(
                synchronize_session=False
            )
            db.commit()

        # 2) Delete old duplicate rows only for keys that still have a newer Asset row.
        active_key_subq = (
            db.query(Asset.storage_key.label("storage_key"))
            .group_by(Asset.storage_key)
            .having(func.max(Asset.created_at) >= cutoff)
            .subquery()
        )
        active_key_query = db.query(active_key_subq.c.storage_key)
        deleted_assets += (
            db.query(Asset)
            .filter(Asset.created_at < cutoff, Asset.storage_key.in_(active_key_query))
            .delete(synchronize_session=False)
        )
        deleted_subtitles += (
            db.query(Subtitle)
            .filter(Subtitle.created_at < cutoff, Subtitle.storage_key.in_(active_key_query))
            .delete(synchronize_session=False)
        )
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
            logger.exception("storage cleanup loop failed")
        _cleanup_stop.wait(timeout=max(30, _cleanup_interval_seconds))


def _start_cleanup_thread() -> None:
    global _cleanup_thread
    if _cleanup_thread and _cleanup_thread.is_alive():
        return
    _cleanup_stop.clear()
    t = threading.Thread(target=_cleanup_loop, name="videoroll-storage-cleanup", daemon=True)
    t.start()
    _cleanup_thread = t


def _effective_youtube_settings(settings: OrchestratorSettings, db: Session, *, cookie_dir: Path | None = None) -> OrchestratorSettings:
    cfg = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    proxy = str(cfg.get("proxy") or "").strip()
    cookies_enabled = bool(cfg.get("cookies_enabled"))

    cookie_file = str(settings.youtube_cookie_file or "").strip() or None
    if cookie_file:
        try:
            if not Path(cookie_file).is_file() and cookie_dir is not None and cookies_enabled:
                # Fall back to the DB-stored cookies when the env-configured cookie file is missing.
                cookie_file = None
        except Exception:
            pass
    if not cookie_file and cookie_dir is not None and cookies_enabled:
        cookies_txt = get_youtube_cookies_txt(db)
        if cookies_txt:
            cookies_txt = normalize_and_validate_netscape_cookies_txt(cookies_txt)
            try:
                cookie_dir.mkdir(parents=True, exist_ok=True)
                cookie_path = cookie_dir / "youtube_cookies.txt"
                cookie_path.write_text(cookies_txt, encoding="utf-8")
                try:
                    os.chmod(cookie_path, 0o600)
                except Exception:
                    pass
                cookie_file = str(cookie_path)
            except Exception:
                cookie_file = None

    return settings.model_copy(update={"youtube_proxy": proxy or None, "youtube_cookie_file": cookie_file})


def _looks_like_youtube_bot_check_error(message: str) -> bool:
    m = str(message or "").lower()
    return (
        "not a bot" in m
        or "unusual traffic" in m
        or "/sorry/" in m
        or "confirm you" in m and "not a bot" in m
    )


def _youtube_bot_check_hint(message: str, *, yt_settings: OrchestratorSettings, db: Session) -> str | None:
    if not _looks_like_youtube_bot_check_error(message):
        return None

    proxy = str(yt_settings.youtube_proxy or "").strip()
    cookie_file = str(yt_settings.youtube_cookie_file or "").strip()

    summary: dict[str, Any] | None = None
    if cookie_file:
        try:
            p = Path(cookie_file)
            if p.is_file():
                summary = summarize_netscape_cookies_txt(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            summary = None

    if summary is None:
        try:
            summary = summarize_netscape_cookies_txt(get_youtube_cookies_txt(db))
        except Exception:
            summary = summarize_netscape_cookies_txt("")

    lines: list[str] = []
    lines.append("提示：YouTube 触发了“确认你不是机器人/异常流量”风控页面（与出口 IP/代理或 cookies 有关）。")
    if proxy:
        lines.append(f"当前代理：{proxy}")
        lines.append("请用同一个代理/IP 在浏览器打开 YouTube，按提示完成登录/验证码后再导出 cookies.txt 更新到系统。")
    else:
        lines.append("如果当前出口 IP 被风控，建议更换网络或配置可用代理后再导出 cookies.txt。")

    if not bool(summary.get("cookies_has_auth")):
        lines.append("你保存的 cookies 看起来不包含登录态（缺少 SID/SAPISID 等）；仅 VISITOR_INFO1_LIVE 这类 cookie 通常不够。")
    if not bool(summary.get("cookies_has_bot_check_bypass")):
        lines.append("若浏览器出现过“确认你不是机器人”页面，需要在同一出口 IP 下通过验证码后再导出（通常会生成 GOOGLE_ABUSE_EXEMPTION）。")
    lines.append("建议用无痕/新会话导出并尽快更新，YouTube 会频繁轮换账号 cookies，旧 cookies 可能很快失效。")
    return "\n".join(lines)


def _youtube_cookie_file_status(settings: OrchestratorSettings) -> tuple[bool, bool]:
    cookie_file = str(settings.youtube_cookie_file or "").strip()
    if not cookie_file:
        return False, False
    try:
        return True, Path(cookie_file).is_file()
    except Exception:
        return True, False


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _secure_cookie(request: Request) -> bool:
    proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    return proto == "https"


def _set_device_cookie(resp: Response, value: str, *, secure: bool) -> None:
    resp.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=value,
        max_age=device_cookie_max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=bool(secure),
        path="/",
    )


def _get_admin_password_hash(request: Request, db: Session | None = None) -> str:
    cached = str(getattr(request.app.state, "admin_password_hash", "") or "").strip()
    if cached:
        return cached

    pw_hash = ""
    if db is not None:
        try:
            pw_hash = str(get_password_hash(db) or "").strip()
        except Exception:
            pw_hash = ""
    else:
        database_url = str(getattr(request.app.state, "database_url", "") or "").strip()
        if database_url:
            SessionLocal = get_sessionmaker(database_url)
            db2 = SessionLocal()
            try:
                pw_hash = str(get_password_hash(db2) or "").strip()
            finally:
                db2.close()

    if pw_hash:
        request.app.state.admin_password_hash = pw_hash
    return pw_hash


@app.get("/auth/status", response_model=AdminAuthStatusRead)
def auth_status(request: Request, db: Session = Depends(get_db)) -> AdminAuthStatusRead:
    pw_hash = _get_admin_password_hash(request, db)
    password_set = bool(pw_hash)

    trusted = False
    if password_set:
        cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
        cookie_val = str(request.cookies.get(DEVICE_COOKIE_NAME) or "").strip()
        if cookie_secret and cookie_val:
            trusted = verify_device_cookie_value(cookie_val, internal_secret=cookie_secret, password_hash=pw_hash)

    return AdminAuthStatusRead(password_set=password_set, trusted=trusted)


@app.post("/auth/setup", response_model=AdminAuthStatusRead)
def auth_setup(payload: AdminAuthSetupRequest, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    existing = _get_admin_password_hash(request, db)
    if existing:
        raise HTTPException(status_code=400, detail="admin password already set")

    pw = validate_new_password(payload.password)
    encoded = encode_password_hash(pw)
    set_password_hash(db, encoded)
    request.app.state.admin_password_hash = encoded

    cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
    cookie_val = mint_device_cookie_value(internal_secret=cookie_secret, password_hash=encoded)
    body = AdminAuthStatusRead(password_set=True, trusted=True).model_dump(mode="json")
    resp = JSONResponse(status_code=200, content=body)
    _set_device_cookie(resp, cookie_val, secure=_secure_cookie(request))
    return resp


@app.post("/auth/login", response_model=AdminAuthStatusRead)
def auth_login(payload: AdminAuthLoginRequest, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    pw_hash = _get_admin_password_hash(request, db)
    if not pw_hash:
        raise HTTPException(status_code=400, detail="admin password is not set")

    if not verify_password_hash(str(payload.password or ""), pw_hash):
        raise HTTPException(status_code=401, detail="invalid password")

    cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
    cookie_val = mint_device_cookie_value(internal_secret=cookie_secret, password_hash=pw_hash)
    body = AdminAuthStatusRead(password_set=True, trusted=True).model_dump(mode="json")
    resp = JSONResponse(status_code=200, content=body)
    _set_device_cookie(resp, cookie_val, secure=_secure_cookie(request))
    return resp


@app.post("/auth/logout", response_model=AdminAuthStatusRead)
def auth_logout(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    pw_hash = _get_admin_password_hash(request, db)
    password_set = bool(pw_hash)
    body = AdminAuthStatusRead(password_set=password_set, trusted=False).model_dump(mode="json")
    resp = JSONResponse(status_code=200, content=body)
    resp.delete_cookie(key=DEVICE_COOKIE_NAME, path="/")
    return resp


def _start_auto_youtube_pipeline(
    *,
    url: str,
    license: SourceLicense,
    proof_url: str | None,
    settings: OrchestratorSettings,
) -> AutoYouTubeResponse:
    url = canonicalize_youtube_url(str(url or "").strip())
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if not _is_youtube_url(url):
        raise HTTPException(status_code=400, detail="url is not a valid youtube url")
    proof = str(proof_url or "").strip() or None

    try:
        with httpx.Client(timeout=30.0, headers=_internal_http_headers(settings)) as client:
            resp = client.post(
                f"{settings.youtube_ingest_url}/youtube/ingest",
                json={"url": url, "license": license.value, "proof_url": proof},
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


@app.post("/auto/youtube", response_model=AutoYouTubeResponse)
def auto_youtube(
    payload: AutoYouTubeRequest,
    settings: OrchestratorSettings = Depends(get_settings),
) -> AutoYouTubeResponse:
    return _start_auto_youtube_pipeline(
        url=payload.url,
        license=payload.license,
        proof_url=payload.proof_url,
        settings=settings,
    )


@app.get(REMOTE_AUTO_YOUTUBE_PATH, response_model=AutoYouTubeResponse)
@app.post(REMOTE_AUTO_YOUTUBE_PATH, response_model=AutoYouTubeResponse)
def remote_auto_youtube(
    url: str | None = Query(default=None),
    token: str | None = Query(default=None, alias=REMOTE_API_TOKEN_QUERY_PARAM),
    license: SourceLicense = Query(default=SourceLicense.authorized),
    proof_url: str | None = Query(default=None),
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AutoYouTubeResponse:
    if not remote_api_token_is_configured(db):
        raise HTTPException(status_code=403, detail="remote api token is not set")
    if not verify_remote_api_token(db, str(token or "")):
        raise HTTPException(status_code=401, detail="invalid remote api token")
    return _start_auto_youtube_pipeline(
        url=str(url or ""),
        license=license,
        proof_url=proof_url,
        settings=settings,
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

    final_assets: list[Asset] = []
    seen: set[uuid.UUID] = set()
    for asset in assets:
        if asset.task_id in seen:
            continue
        seen.add(asset.task_id)
        final_assets.append(asset)
        if len(final_assets) >= limit:
            break

    task_ids = [asset.task_id for asset in final_assets]
    if not task_ids:
        return []

    task_map = {task.id: task for task in db.query(Task).filter(Task.id.in_(task_ids)).all()}
    cover_assets = (
        db.query(Asset)
        .filter(Asset.task_id.in_(task_ids), Asset.kind == AssetKind.cover_image)
        .order_by(Asset.created_at.desc())
        .all()
    )
    cover_by_task: dict[uuid.UUID, Asset] = {}
    for asset in cover_assets:
        if asset.task_id not in cover_by_task:
            cover_by_task[asset.task_id] = asset

    title_map = _load_task_display_titles(db, task_ids, allow_s3_fallback=False)

    items: list[dict[str, Any]] = []
    for asset in final_assets:
        task = task_map.get(asset.task_id)
        if not task:
            continue
        display_title = str(title_map.get(asset.task_id) or "").strip()
        items.append(
            {
                "task": task,
                "final_asset": asset,
                "cover_asset": cover_by_task.get(asset.task_id),
                "display_title": display_title or None,
            }
        )
    return items


@app.get("/tasks", response_model=list[TaskRead])
def list_tasks(
    status: Optional[TaskStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> list[dict[str, Any]]:
    q = db.query(Task).order_by(Task.created_at.desc())
    if status is not None:
        q = q.filter(Task.status == status)
    tasks = q.limit(limit).all()

    ids = [t.id for t in tasks]
    title_map = _load_task_display_titles(db, ids, s3=s3, allow_s3_fallback=True)

    out: list[dict[str, Any]] = []
    for t in tasks:
        item = TaskRead.model_validate(t).model_dump()
        display_title = str(title_map.get(t.id) or "").strip()
        item["display_title"] = display_title or None
        out.append(item)
    return out


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


@app.get("/settings/api", response_model=RemoteAPISettingsRead)
def get_remote_api_settings_view(db: Session = Depends(get_db)) -> RemoteAPISettingsRead:
    return RemoteAPISettingsRead(**get_remote_api_settings(db))


@app.put("/settings/api", response_model=RemoteAPISettingsRead)
def put_remote_api_settings_view(payload: RemoteAPISettingsUpdate, db: Session = Depends(get_db)) -> RemoteAPISettingsRead:
    cfg = update_remote_api_settings(db, payload.model_dump(exclude_unset=True))
    return RemoteAPISettingsRead(**cfg)


@app.get("/settings/youtube", response_model=YouTubeSettingsRead)
def get_youtube_settings_view(settings: OrchestratorSettings = Depends(get_settings), db: Session = Depends(get_db)) -> YouTubeSettingsRead:
    cfg = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    cookie_file_configured, cookie_file_exists = _youtube_cookie_file_status(settings)
    return YouTubeSettingsRead(**cfg, cookie_file_configured=cookie_file_configured, cookie_file_exists=cookie_file_exists)


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
    cookie_file_configured, cookie_file_exists = _youtube_cookie_file_status(settings)
    return YouTubeSettingsRead(**cfg, cookie_file_configured=cookie_file_configured, cookie_file_exists=cookie_file_exists)


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
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    item = TaskRead.model_validate(task).model_dump()
    title = get_task_display_title_with_s3(db, str(task_id), s3=s3).strip()
    item["display_title"] = title or None
    return item


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
    return await _store_uploaded_task_asset(
        task=task,
        file=file,
        s3=s3,
        db=db,
        temp_prefix="videoroll_",
        default_suffix=".mp4",
        key_prefix="raw",
        object_name_prefix="video",
        asset_kind=AssetKind.video_raw,
        update_task_status=TaskStatus.downloaded,
    )


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
    return await _store_uploaded_task_asset(
        task=task,
        file=file,
        s3=s3,
        db=db,
        temp_prefix="videoroll_cover_",
        default_suffix=".jpg",
        key_prefix="final",
        object_name_prefix="cover",
        asset_kind=AssetKind.cover_image,
    )


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

    try:
        resp = s3.get_object(asset.storage_key)
    except ClientError as e:
        if _is_s3_object_missing(e):
            raise HTTPException(status_code=404, detail="asset object not found") from e
        raise
    body = resp["Body"]
    media_type = resp.get("ContentType") or "application/octet-stream"
    filename = _suggest_asset_filename(db, task_id, asset, s3=s3)

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

    filename = _suggest_asset_filename(db, task_id, asset, s3=s3)

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

        try:
            resp = s3.get_object(asset.storage_key, range_bytes=f"bytes={start}-{end}")
        except ClientError as e:
            if _is_s3_object_missing(e):
                raise HTTPException(status_code=404, detail="asset object not found") from e
            raise
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

    try:
        resp = s3.get_object(asset.storage_key)
    except ClientError as e:
        if _is_s3_object_missing(e):
            raise HTTPException(status_code=404, detail="asset object not found") from e
        raise
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
    remaining_final = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_final, Asset.id != asset_id)
        .first()
    )
    db.delete(asset)
    db.commit()
    return {"deleted": True}


def _is_youtube_url(url: str) -> bool:
    return is_youtube_url(url)


@app.get("/tasks/{task_id}/youtube_meta", response_model=YouTubeMetaRead)
def get_cached_youtube_meta(task_id: uuid.UUID, db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)) -> YouTubeMetaRead:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_type.value != "youtube":
        raise HTTPException(status_code=400, detail="task is not a youtube source")

    latest = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not latest:
        raise HTTPException(status_code=404, detail="youtube meta not found")

    try:
        raw = _read_s3_bytes(s3, latest.storage_key)
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        info = parsed if isinstance(parsed, dict) else {}
        meta = summarize_info(info, fallback_url=str(task.source_url or "").strip())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"failed to read youtube meta: {e}") from e

    webpage_url = str(meta.webpage_url or "").strip()
    if not webpage_url:
        raise HTTPException(status_code=400, detail="task.source_url is empty")

    return _youtube_meta_to_read(meta)


@app.get("/tasks/{task_id}/publish_meta")
def get_task_publish_meta(task_id: uuid.UUID, db: Session = Depends(get_db), s3: S3Store = Depends(get_s3)) -> dict[str, Any]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    key = _publish_meta_s3_key(task_id)
    obj = _read_s3_json_object(s3, key)
    if obj is None:
        raise HTTPException(status_code=404, detail="publish_meta not found")
    return obj


@app.put("/tasks/{task_id}/publish_meta")
def put_task_publish_meta(
    task_id: uuid.UUID,
    meta: dict[str, Any],
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> dict[str, Any]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    if not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="meta must be an object")
    key = _publish_meta_s3_key(task_id)
    try:
        _write_s3_json(s3, key, meta)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"failed to write publish_meta: {e}") from e
    return {"stored": True, "key": key}


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

    yt_settings: OrchestratorSettings = settings

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
        work_root = Path(settings.work_dir) / "youtube" / str(task_id)
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ytmeta_", dir=str(work_root)) as tmp:
            try:
                yt_settings = _effective_youtube_settings(settings, db, cookie_dir=Path(tmp))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"invalid youtube cookies: {e}") from e
            info, meta = extract_youtube_metadata(url, yt_settings)
    except HTTPException:
        raise
    except Exception as e:
        hint = _youtube_bot_check_hint(str(e), yt_settings=yt_settings, db=db)
        detail = f"youtube metadata failed: {e}"
        if hint:
            detail = f"{detail}\n\n{hint}"
        raise HTTPException(status_code=502, detail=detail) from e

    payload = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
    sha = _sha256_bytes(payload)
    key = f"raw/{task_id}/metadata_{sha[:16]}.json"
    s3.put_bytes(payload, key, content_type="application/json")

    asset = Asset(
        task_id=task_id,
        kind=AssetKind.metadata_json,
        storage_key=key,
        sha256=sha,
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

    work_root = Path(settings.work_dir) / "youtube" / str(task_id)
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ytdlp_", dir=str(work_root)) as tmp:
        tmp_dir = Path(tmp)
        try:
            yt_settings = _effective_youtube_settings(settings, db, cookie_dir=tmp_dir)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid youtube cookies: {e}") from e
        video_asset = existing_video
        info: dict[str, Any] = {}
        meta: Any = None

        if video_asset is None:
            try:
                video_path, info, meta = download_youtube_video(url, yt_settings, work_dir=tmp_dir)
            except Exception as e:
                hint = _youtube_bot_check_hint(str(e), yt_settings=yt_settings, db=db)
                diagnostics = e.diagnostics if isinstance(e, YtDlpRuntimeError) else []
                try:
                    _store_task_log_asset(
                        db,
                        s3,
                        task_id=task_id,
                        log_key=_youtube_download_log_key(task_id),
                        text=_build_youtube_download_failure_log(
                            task_id=task_id,
                            url=url,
                            error_message=str(e),
                            hint=hint,
                            diagnostics=diagnostics,
                        ),
                    )
                except Exception:
                    db.rollback()
                    logger.exception("failed to persist youtube download diagnostics log for task %s", task_id)
                detail = f"youtube download failed: {e}"
                if hint:
                    detail = f"{detail}\n\n{hint}"
                raise HTTPException(status_code=502, detail=detail) from e

            suffix = video_path.suffix.lower() or ".mp4"
            video_sha = sha256_file(video_path)
            video_key = f"raw/{task_id}/video_{video_sha[:16]}{suffix}"
            s3.upload_file(video_path, video_key)
            video_asset = Asset(
                task_id=task_id,
                kind=AssetKind.video_raw,
                storage_key=video_key,
                sha256=video_sha,
                size_bytes=video_path.stat().st_size,
            )
            db.add(video_asset)
        else:
            # Prefer cached metadata to avoid extra YouTube requests.
            latest_meta_asset = (
                db.query(Asset)
                .filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json)
                .order_by(Asset.created_at.desc())
                .first()
            )

            info_cached: dict[str, Any] | None = None
            if latest_meta_asset:
                try:
                    raw = _read_s3_bytes(s3, latest_meta_asset.storage_key)
                    parsed = json.loads(raw.decode("utf-8")) if raw else {}
                    info_cached = parsed if isinstance(parsed, dict) else None
                except Exception:
                    info_cached = None

            if info_cached is not None:
                info = _as_dict(info_cached)
                meta = summarize_info(_as_dict(info_cached), fallback_url=url)
            else:
                try:
                    info, meta = extract_youtube_metadata(url, yt_settings)
                except Exception as e:
                    raise HTTPException(status_code=502, detail=f"youtube metadata failed: {e}") from e

        meta_payload = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
        meta_sha = _sha256_bytes(meta_payload)
        meta_key = f"raw/{task_id}/metadata_{meta_sha[:16]}.json"
        s3.put_bytes(meta_payload, meta_key, content_type="application/json")
        meta_asset = Asset(
            task_id=task_id,
            kind=AssetKind.metadata_json,
            storage_key=meta_key,
            sha256=meta_sha,
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

        if cover_asset is None:
            cover_asset = (
                db.query(Asset)
                .filter(Asset.task_id == task_id, Asset.kind == AssetKind.cover_image)
                .order_by(Asset.created_at.desc())
                .first()
            )

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
        resp = _as_dict(j.response_json)
        typeid_obj = _as_dict(resp.get("typeid"))
        ai_obj = _as_dict(typeid_obj.get("ai"))

        typeid_mode = str(typeid_obj.get("mode") or _as_dict(j.meta_json).get("typeid_mode") or "").strip() or None
        typeid_selected_by = str(typeid_obj.get("selected_by") or "").strip() or None

        tid_val = typeid_obj.get("selected") if typeid_obj else resp.get("tid")
        try:
            tid = int(tid_val) if tid_val is not None else None
        except Exception:
            tid = None
        if tid is not None and tid <= 0:
            tid = None

        typeid_ai_ok = ai_obj.get("ok") if "ok" in ai_obj else None
        if typeid_ai_ok is not None:
            typeid_ai_ok = bool(typeid_ai_ok)
        typeid_ai_reason = str(ai_obj.get("reason") or "").strip() or None

        out.append(
            {
                "id": j.id,
                "task_id": j.task_id,
                "state": j.state.value,
                "aid": j.aid,
                "bvid": j.bvid,
                "tid": tid,
                "typeid_mode": typeid_mode,
                "typeid_selected_by": typeid_selected_by,
                "typeid_ai_ok": typeid_ai_ok,
                "typeid_ai_reason": typeid_ai_reason,
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

    # Avoid creating duplicate jobs for the same task when a job is already queued/running.
    # This commonly happens when the user clicks multiple times and results in a "queued" job
    # that won't start until the current one finishes.
    in_flight = (
        db.query(SubtitleJob)
        .filter(
            SubtitleJob.task_id == task_id,
            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
        )
        .order_by(SubtitleJob.created_at.desc())
        .first()
    )
    if in_flight:
        # Best-effort scheduler kick: helps when a job is queued but a tick was dropped.
        try:
            with httpx.Client(timeout=5.0, headers=_internal_http_headers(settings)) as client:
                client.post(f"{settings.subtitle_service_url}/subtitle/task_queue/tick")
        except httpx.HTTPError:
            pass
        return RemoteJobResponse(job_id=in_flight.id, status=in_flight.status.value)

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
        "resume": bool(payload.resume),
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
                "video_preset": payload.video_preset,
                "video_crf": payload.video_crf,
            },
        },
        "output_prefix": f"sub/{task_id}/",
    }

    try:
        with httpx.Client(timeout=30.0, headers=_internal_http_headers(settings)) as client:
            resp = client.post(f"{settings.subtitle_service_url}/subtitle/jobs", json=req)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"subtitle-service request failed: {e}") from e

    return RemoteJobResponse(job_id=uuid.UUID(data["job_id"]), status=str(data.get("status", "queued")))


@app.post("/tasks/{task_id}/actions/subtitle_resume", response_model=RemoteJobResponse)
def resume_subtitle_job(
    task_id: uuid.UUID,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RemoteJobResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    in_flight = (
        db.query(SubtitleJob)
        .filter(
            SubtitleJob.task_id == task_id,
            SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]),
        )
        .count()
    )
    if in_flight:
        raise HTTPException(status_code=409, detail="subtitle job already in progress")

    prev = (
        db.query(SubtitleJob)
        .filter(SubtitleJob.task_id == task_id)
        .order_by(SubtitleJob.created_at.desc())
        .first()
    )
    if not prev:
        raise HTTPException(status_code=400, detail="no subtitle job found to resume")

    req_in = prev.request_json if isinstance(prev.request_json, dict) else {}
    if not req_in:
        raise HTTPException(status_code=400, detail="subtitle job request is empty")

    req_out = dict(req_in)
    req_out["task_id"] = str(task_id)
    req_out["resume"] = True
    if not isinstance(req_out.get("output_prefix"), str) or not str(req_out.get("output_prefix") or "").strip():
        req_out["output_prefix"] = f"sub/{task_id}/"

    try:
        with httpx.Client(timeout=30.0, headers=_internal_http_headers(settings)) as client:
            resp = client.post(f"{settings.subtitle_service_url}/subtitle/jobs", json=req_out)
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
    s3: S3Store = Depends(get_s3),
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

    if payload.meta is None:
        stored = _read_s3_json_object(s3, _publish_meta_s3_key(task_id))
        if stored is None:
            raise HTTPException(status_code=400, detail="meta is missing and publish_meta is not found")
        meta = dict(stored)
    else:
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

    # Persist the final publish meta so publish is reproducible and editable before/after.
    try:
        _write_s3_json(s3, _publish_meta_s3_key(task_id), meta)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"failed to persist publish_meta: {e}") from e

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
        with httpx.Client(timeout=30.0, headers=_internal_http_headers(settings)) as client:
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
