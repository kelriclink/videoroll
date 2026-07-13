from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.schemas import (
    AutoYouTubeResponse,
    AutoYouTubeTaskStartResponse,
    YouTubeDownloadActionResponse,
    YouTubeHomeScanRunResponse,
    YouTubeMetaActionResponse,
    YouTubeMetaRead,
    YouTubeProxyTestResponse,
)
from videoroll.apps.orchestrator_api.infrastructure.internal_http import (
    InternalServiceResponse,
    internal_http_headers,
    proxy_internal_service_request,
)
from videoroll.apps.orchestrator_api.services.asset_service import (
    queue_pending_s3_delete,
    read_s3_bytes,
    write_s3_text,
)
from videoroll.apps.orchestrator_api.youtube_downloader import (
    YtDlpRuntimeError,
    download_thumbnail_jpg,
    download_youtube_video,
    extract_youtube_metadata,
    summarize_info,
)
from videoroll.apps.orchestrator_api.youtube_home_feed import fetch_youtube_home_feed
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.youtube_settings_store import (
    finish_youtube_home_scan_lock,
    get_youtube_cookies_txt,
    get_youtube_settings,
    normalize_and_validate_netscape_cookies_txt,
    summarize_netscape_cookies_txt,
    try_acquire_youtube_home_scan_lock,
)
from videoroll.config import OrchestratorSettings
from videoroll.db.models import (
    Asset,
    AssetKind,
    IngestedVideo,
    RenderJob,
    RenderJobStatus,
    SourceLicense,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.utils.auto_youtube import encode_auto_youtube_created_by
from videoroll.utils.hashing import sha256_file
from videoroll.utils.httpx_proxy import HTTPX_PROXY_KWARG_UNSUPPORTED, format_httpx_proxy_error
from videoroll.utils.youtube_urls import canonicalize_youtube_url, is_youtube_url


logger = logging.getLogger(__name__)


_BROWSER_PROXY_PATHS: dict[str, set[str]] = {
    "GET": {"youtube/sources"},
    "POST": {"youtube/ingest", "youtube/sources"},
}


async def proxy_browser_request(
    settings: OrchestratorSettings,
    *,
    service_path: str,
    method: str,
    query_string: str,
    body: bytes,
    content_type: str | None,
) -> InternalServiceResponse:
    normalized_method = method.upper()
    allowed = service_path in _BROWSER_PROXY_PATHS.get(normalized_method, set())
    if normalized_method in {"PATCH", "DELETE"}:
        allowed = service_path.startswith("youtube/sources/") and service_path.count("/") == 2
    if normalized_method == "POST":
        allowed = allowed or (
            service_path.startswith("youtube/sources/")
            and service_path.endswith("/scan")
            and service_path.count("/") == 3
        )
    if not allowed:
        raise HTTPException(status_code=404, detail="youtube browser operation not found")
    return await proxy_internal_service_request(
        settings,
        service_url=settings.youtube_ingest_url,
        service_path=service_path,
        method=normalized_method,
        query_string=query_string,
        body=body,
        content_type=content_type,
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_storage_key(prefix: str, digest: str, suffix: str) -> str:
    return f"{prefix}_{digest[:16]}_{uuid.uuid4().hex[:12]}{suffix}"


def _queue_uploaded_objects_for_cleanup(db: Session, storage_keys: list[str]) -> None:
    for storage_key in reversed(storage_keys):
        try:
            queue_pending_s3_delete(db, storage_key, reason="failed_youtube_upload")
        except Exception:
            db.rollback()
            logger.exception("failed to queue uploaded YouTube object cleanup", extra={"storage_key": storage_key})


def _storage_key_is_referenced(db: Session, storage_key: str) -> bool:
    return db.query(Asset.id).filter(Asset.storage_key == storage_key).first() is not None


def youtube_meta_to_read(meta: Any) -> YouTubeMetaRead:
    return YouTubeMetaRead(
        title=str(getattr(meta, "title", "") or ""),
        description=str(getattr(meta, "description", "") or ""),
        webpage_url=str(getattr(meta, "webpage_url", "") or ""),
        uploader=getattr(meta, "uploader", None),
        upload_date=getattr(meta, "upload_date", None),
        duration=getattr(meta, "duration", None),
    )


def set_task_created_by(settings: OrchestratorSettings, *, task_id: uuid.UUID, created_by: str | None) -> None:
    marker = str(created_by or "").strip()
    if not marker:
        return
    db = get_sessionmaker(settings.database_url)()
    try:
        task = db.get(Task, task_id)
        if not task:
            return
        task.created_by = marker
        db.add(task)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("failed to persist created_by for task %s", task_id)
    finally:
        db.close()


def ingest_youtube_source(*, url: str, license: SourceLicense, proof_url: str | None, settings: OrchestratorSettings) -> tuple[uuid.UUID, bool, str | None]:
    normalized_url = canonicalize_youtube_url(str(url or "").strip())
    if not normalized_url:
        raise HTTPException(status_code=400, detail="url is required")
    if not is_youtube_url(normalized_url):
        raise HTTPException(status_code=400, detail="url is not a valid youtube url")
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            response = client.post(
                f"{settings.youtube_ingest_url}/youtube/ingest",
                json={"url": normalized_url, "license": license.value, "proof_url": str(proof_url or "").strip() or None},
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=f"youtube-ingest: {exc.response.text}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"youtube-ingest request failed: {exc}") from exc
    try:
        task_id = uuid.UUID(str(data.get("task_id")))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"youtube-ingest returned invalid task_id: {data}") from exc
    return task_id, bool(data.get("deduped")), str(data.get("source_id") or "") or None


def enqueue_auto_youtube_pipeline(task_id: uuid.UUID, *, auto_publish: bool | None) -> str:
    from videoroll.apps.subtitle_service.worker import celery_app

    args: list[Any] = [str(task_id)]
    if auto_publish is not None:
        args.append({"auto_publish": bool(auto_publish)})
    return str(celery_app.send_task("subtitle_service.auto_youtube_pipeline", args=args, queue="subtitle").id)


def start_auto_youtube_pipeline(*, url: str, license: SourceLicense, proof_url: str | None, auto_publish: bool | None, settings: OrchestratorSettings) -> AutoYouTubeResponse:
    task_id, deduped, source_id = ingest_youtube_source(url=url, license=license, proof_url=proof_url, settings=settings)
    set_task_created_by(
        settings,
        task_id=task_id,
        created_by=encode_auto_youtube_created_by("auto_youtube", auto_publish=auto_publish),
    )
    return AutoYouTubeResponse(
        task_id=task_id,
        pipeline_job_id=enqueue_auto_youtube_pipeline(task_id, auto_publish=auto_publish),
        deduped=deduped,
        source_id=source_id,
    )


def start_existing_task(task_id: uuid.UUID, *, settings: OrchestratorSettings, db: Session) -> AutoYouTubeTaskStartResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_type.value != "youtube" or not str(task.source_url or "").strip():
        raise HTTPException(status_code=400, detail="task is not a youtube source")
    if task.status in {TaskStatus.published, TaskStatus.publishing, TaskStatus.canceled}:
        raise HTTPException(status_code=400, detail=f"task status={task.status.value.lower()} cannot start auto youtube pipeline")
    subtitle_inflight = db.query(SubtitleJob).filter(SubtitleJob.task_id == task_id, SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running])).count()
    render_inflight = db.query(RenderJob).filter(RenderJob.task_id == task_id, RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running])).count()
    if subtitle_inflight or render_inflight:
        raise HTTPException(status_code=409, detail="subtitle/render job already in progress for this task")
    auto_publish = bool(get_auto_profile(db).get("auto_publish"))
    set_task_created_by(settings, task_id=task_id, created_by=encode_auto_youtube_created_by("youtube_task_restart", auto_publish=auto_publish))
    return AutoYouTubeTaskStartResponse(task_id=task_id, pipeline_job_id=enqueue_auto_youtube_pipeline(task_id, auto_publish=auto_publish))


def effective_youtube_settings(settings: OrchestratorSettings, db: Session, *, cookie_dir: Path | None = None) -> OrchestratorSettings:
    config = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
    proxy = str(config.get("proxy") or "").strip()
    enabled = bool(config.get("cookies_enabled"))
    cookie_file = str(settings.youtube_cookie_file or "").strip() or None
    if cookie_file and cookie_dir is not None and enabled and not Path(cookie_file).is_file():
        cookie_file = None
    if not cookie_file and cookie_dir is not None and enabled:
        cookies = get_youtube_cookies_txt(db)
        if cookies:
            cookies = normalize_and_validate_netscape_cookies_txt(cookies)
            cookie_dir.mkdir(parents=True, exist_ok=True)
            path = cookie_dir / "youtube_cookies.txt"
            path.write_text(cookies, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            cookie_file = str(path)
    return settings.model_copy(update={"youtube_proxy": proxy or None, "youtube_cookie_file": cookie_file})


def youtube_bot_check_hint(message: str, *, yt_settings: OrchestratorSettings, db: Session) -> str | None:
    lowered = str(message or "").lower()
    if not ("not a bot" in lowered or "unusual traffic" in lowered or "/sorry/" in lowered):
        return None
    proxy = str(yt_settings.youtube_proxy or "").strip()
    summary = None
    cookie_file = str(yt_settings.youtube_cookie_file or "").strip()
    if cookie_file and Path(cookie_file).is_file():
        summary = summarize_netscape_cookies_txt(Path(cookie_file).read_text(encoding="utf-8", errors="ignore"))
    if summary is None:
        summary = summarize_netscape_cookies_txt(get_youtube_cookies_txt(db))
    lines = ["提示：YouTube 触发了“确认你不是机器人/异常流量”风控页面（与出口 IP/代理或 cookies 有关）。"]
    lines.append(f"当前代理：{proxy}" if proxy else "如果当前出口 IP 被风控，建议更换网络或配置可用代理后再导出 cookies.txt。")
    if not summary.get("cookies_has_auth"):
        lines.append("你保存的 cookies 看起来不包含登录态（缺少 SID/SAPISID 等）；仅 VISITOR_INFO1_LIVE 这类 cookie 通常不够。")
    if not summary.get("cookies_has_bot_check_bypass"):
        lines.append("若浏览器出现过“确认你不是机器人”页面，需要在同一出口 IP 下通过验证码后再导出。")
    return "\n".join(lines)


def get_cached_meta(task_id: uuid.UUID, *, db: Session, s3: S3Store) -> YouTubeMetaRead:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_type.value != "youtube":
        raise HTTPException(status_code=400, detail="task is not a youtube source")
    asset = db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json).order_by(Asset.created_at.desc()).first()
    if not asset:
        raise HTTPException(status_code=404, detail="youtube meta not found")
    try:
        raw = read_s3_bytes(s3, asset.storage_key)
        info = json.loads(raw.decode("utf-8")) if raw else {}
        meta = summarize_info(info if isinstance(info, dict) else {}, fallback_url=str(task.source_url or "").strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to read youtube meta: {exc}") from exc
    return youtube_meta_to_read(meta)


def fetch_meta(task_id: uuid.UUID, *, settings: OrchestratorSettings, db: Session, s3: S3Store) -> YouTubeMetaActionResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    url = str(task.source_url or "").strip()
    if task.source_type.value != "youtube":
        raise HTTPException(status_code=400, detail="task is not a youtube source")
    if not url or not is_youtube_url(url):
        raise HTTPException(status_code=400, detail="task.source_url is empty" if not url else "task.source_url is not a valid youtube url")
    latest = db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json).order_by(Asset.created_at.desc()).first()
    if latest:
        try:
            info = json.loads(read_s3_bytes(s3, latest.storage_key).decode("utf-8"))
            meta = summarize_info(info if isinstance(info, dict) else {}, fallback_url=url)
            return YouTubeMetaActionResponse(metadata=youtube_meta_to_read(meta), metadata_asset=latest)
        except Exception:
            pass
    yt_settings = settings
    try:
        root = Path(settings.work_dir) / "youtube" / str(task_id)
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ytmeta_", dir=str(root)) as temp:
            yt_settings = effective_youtube_settings(settings, db, cookie_dir=Path(temp))
            info, meta = extract_youtube_metadata(url, yt_settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid youtube cookies: {exc}") from exc
    except Exception as exc:
        hint = youtube_bot_check_hint(str(exc), yt_settings=yt_settings, db=db)
        raise HTTPException(status_code=502, detail=f"youtube metadata failed: {exc}" + (f"\n\n{hint}" if hint else "")) from exc
    payload = json.dumps(info, ensure_ascii=False, indent=2).encode()
    digest = _sha256_bytes(payload)
    key = _unique_storage_key(f"raw/{task_id}/metadata", digest, ".json")
    key_was_referenced = _storage_key_is_referenced(db, key)
    s3.put_bytes(payload, key, content_type="application/json")
    asset = Asset(task_id=task_id, kind=AssetKind.metadata_json, storage_key=key, sha256=digest, size_bytes=len(payload))
    db.add(asset)
    try:
        db.commit()
        db.refresh(asset)
    except Exception:
        db.rollback()
        if not key_was_referenced:
            _queue_uploaded_objects_for_cleanup(db, [key])
        raise
    return YouTubeMetaActionResponse(metadata=youtube_meta_to_read(meta), metadata_asset=asset)


def _store_failure_log(db: Session, s3: S3Store, task_id: uuid.UUID, url: str, exc: Exception, hint: str | None) -> None:
    diagnostics = exc.diagnostics if isinstance(exc, YtDlpRuntimeError) else []
    text = "\n".join(diagnostics).strip() or "videoroll yt-dlp diagnostics unavailable"
    text += f"\n\n---- videoroll error summary ----\ntask_id={task_id}\nurl={url}\nerror={exc}\n"
    if hint:
        text += f"\n---- videoroll hint ----\n{hint}\n"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"log/{task_id}/youtube_download_{stamp}_{uuid.uuid4().hex[:8]}.log"
    payload = write_s3_text(s3, key, text)
    db.add(Asset(task_id=task_id, kind=AssetKind.log, storage_key=key, sha256=_sha256_bytes(payload), size_bytes=len(payload)))
    db.commit()


def download(task_id: uuid.UUID, *, settings: OrchestratorSettings, db: Session, s3: S3Store) -> YouTubeDownloadActionResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    url = str(task.source_url or "").strip()
    if task.source_type.value != "youtube":
        raise HTTPException(status_code=400, detail="task is not a youtube source")
    if not url or not is_youtube_url(url):
        raise HTTPException(status_code=400, detail="task.source_url is empty" if not url else "task.source_url is not a valid youtube url")
    video_asset = db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_raw).order_by(Asset.created_at.desc()).first()
    uploaded_keys: list[str] = []
    root = Path(settings.work_dir) / "youtube" / str(task_id); root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ytdlp_", dir=str(root)) as temp:
        temp_dir = Path(temp)
        try:
            yt_settings = effective_youtube_settings(settings, db, cookie_dir=temp_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid youtube cookies: {exc}") from exc
        info: dict[str, Any] = {}; meta: Any = None
        if video_asset is None:
            try:
                video_path, info, meta = download_youtube_video(url, yt_settings, work_dir=temp_dir)
            except Exception as exc:
                hint = youtube_bot_check_hint(str(exc), yt_settings=yt_settings, db=db)
                try:
                    _store_failure_log(db, s3, task_id, url, exc, hint)
                except Exception:
                    db.rollback()
                raise HTTPException(status_code=502, detail=f"youtube download failed: {exc}" + (f"\n\n{hint}" if hint else "")) from exc
            digest = sha256_file(video_path)
            key = _unique_storage_key(
                f"raw/{task_id}/video",
                digest,
                video_path.suffix.lower() or ".mp4",
            )
            key_was_referenced = _storage_key_is_referenced(db, key)
            s3.upload_file(video_path, key)
            if not key_was_referenced:
                uploaded_keys.append(key)
            video_asset = Asset(task_id=task_id, kind=AssetKind.video_raw, storage_key=key, sha256=digest, size_bytes=video_path.stat().st_size); db.add(video_asset)
        else:
            latest = db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json).order_by(Asset.created_at.desc()).first()
            try:
                info = json.loads(read_s3_bytes(s3, latest.storage_key).decode()) if latest else {}
                if not isinstance(info, dict) or not info:
                    raise ValueError
                meta = summarize_info(info, fallback_url=url)
            except Exception:
                info, meta = extract_youtube_metadata(url, yt_settings)
        payload = json.dumps(info, ensure_ascii=False, indent=2).encode(); digest = _sha256_bytes(payload)
        key = _unique_storage_key(f"raw/{task_id}/metadata", digest, ".json")
        key_was_referenced = _storage_key_is_referenced(db, key)
        s3.put_bytes(payload, key, content_type="application/json")
        if not key_was_referenced:
            uploaded_keys.append(key)
        meta_asset = Asset(task_id=task_id, kind=AssetKind.metadata_json, storage_key=key, sha256=digest, size_bytes=len(payload)); db.add(meta_asset)
        cover_asset = None
        try:
            cover_path = download_thumbnail_jpg(info, yt_settings, work_dir=temp_dir)
            if cover_path:
                cover_digest = sha256_file(cover_path)
                cover_asset = db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.cover_image, Asset.sha256 == cover_digest).first()
                if not cover_asset:
                    cover_key = _unique_storage_key(
                        f"final/{task_id}/cover_youtube",
                        cover_digest,
                        ".jpg",
                    )
                    key_was_referenced = _storage_key_is_referenced(db, cover_key)
                    s3.upload_file(cover_path, cover_key, content_type="image/jpeg")
                    if not key_was_referenced:
                        uploaded_keys.append(cover_key)
                    cover_asset = Asset(task_id=task_id, kind=AssetKind.cover_image, storage_key=cover_key, sha256=cover_digest, size_bytes=cover_path.stat().st_size); db.add(cover_asset)
        except Exception:
            cover_asset = db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.cover_image).order_by(Asset.created_at.desc()).first()
        if task.status in {TaskStatus.created, TaskStatus.ingested}:
            task.status = TaskStatus.downloaded; db.add(task)
        try:
            db.commit()
            db.refresh(meta_asset)
            db.refresh(video_asset)
            if cover_asset:
                db.refresh(cover_asset)
        except Exception:
            db.rollback()
            _queue_uploaded_objects_for_cleanup(db, uploaded_keys)
            raise
    return YouTubeDownloadActionResponse(metadata=youtube_meta_to_read(meta), metadata_asset=meta_asset, video_asset=video_asset, cover_asset=cover_asset)


def test_proxy(*, url: str, proxy: str, settings: OrchestratorSettings) -> YouTubeProxyTestResponse:
    started = time.perf_counter(); headers = {"User-Agent": settings.youtube_user_agent}
    kwargs: dict[str, Any] = {"timeout": 20.0, "follow_redirects": True, "headers": headers}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        try:
            with httpx.Client(**kwargs) as client:
                response = client.get(url)
                return YouTubeProxyTestResponse(ok=response.status_code < 400, url=url, used_proxy=proxy or None, status_code=response.status_code, elapsed_ms=int((time.perf_counter() - started) * 1000))
        except TypeError:
            with httpx.Client(timeout=20.0, follow_redirects=True, headers=headers) as client:
                response = client.get(url)
                return YouTubeProxyTestResponse(ok=response.status_code < 400, url=url, used_proxy=None, status_code=response.status_code, elapsed_ms=int((time.perf_counter() - started) * 1000), error=HTTPX_PROXY_KWARG_UNSUPPORTED)
    except Exception as exc:
        return YouTubeProxyTestResponse(ok=False, url=url, used_proxy=proxy or None, elapsed_ms=int((time.perf_counter() - started) * 1000), error=format_httpx_proxy_error(exc, proxy=proxy))


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def home_scan_is_due(config: dict[str, Any], *, now: datetime | None = None) -> bool:
    current = now or datetime.now(tz=timezone.utc)
    if not bool(config.get("home_scan_enabled")):
        return False
    interval_minutes = max(1, int(config.get("home_scan_interval_minutes") or 60))
    last_finished = _parse_iso_datetime(config.get("home_scan_last_finished_at"))
    last_started = _parse_iso_datetime(config.get("home_scan_last_started_at"))
    baseline = last_finished or last_started
    if baseline is None:
        return True
    return current >= baseline + timedelta(minutes=interval_minutes)


def run_home_scan(settings: OrchestratorSettings, *, force: bool = False, raise_if_locked: bool = False) -> YouTubeHomeScanRunResponse | None:
    db = get_sessionmaker(settings.database_url)(); owner = f"youtube-router:{os.getpid()}:{uuid.uuid4().hex[:8]}"; acquired = False
    try:
        config = get_youtube_settings(db, default_proxy=settings.youtube_proxy)
        if not force and not home_scan_is_due(config):
            return None
        if not try_acquire_youtube_home_scan_lock(db, owner=owner, ttl_seconds=int(os.getenv("YOUTUBE_HOME_SCAN_LOCK_TTL_SECONDS", "900"))):
            if raise_if_locked:
                raise RuntimeError("youtube home scan is already running")
            return None
        acquired = True
        cookies = ""
        cookie_file = str(settings.youtube_cookie_file or "").strip()
        if cookie_file and Path(cookie_file).is_file():
            cookies = Path(cookie_file).read_text(encoding="utf-8", errors="ignore")
        if not cookies and config.get("cookies_enabled"):
            cookies = get_youtube_cookies_txt(db)
        if not cookies:
            raise RuntimeError("youtube cookies are not configured; save cookies.txt or set YOUTUBE_COOKIE_FILE first")
        limit = max(1, min(int(config.get("home_scan_limit") or 10), 100))
        feed = fetch_youtube_home_feed(cookies, settings.youtube_user_agent, proxy=str(config.get("proxy") or "").strip() or None, limit=limit, long_videos_only=bool(config.get("home_scan_long_videos_only")), min_duration_seconds=max(0, int(config.get("home_scan_min_duration_seconds") or 0)), timezone_name=str(os.getenv("TZ") or "UTC"))
        profile = get_auto_profile(db); auto_publish = bool(profile.get("auto_publish")); created = []; jobs = []; skipped = 0; failed = 0; errors = []
        for item in feed.videos:
            if db.query(IngestedVideo).filter(IngestedVideo.platform == "youtube", IngestedVideo.source_id == item.video_id).first():
                skipped += 1; continue
            try:
                task_id, deduped, _ = ingest_youtube_source(url=item.url, license=SourceLicense.authorized, proof_url=None, settings=settings)
                if deduped:
                    skipped += 1; continue
                set_task_created_by(settings, task_id=task_id, created_by=encode_auto_youtube_created_by("youtube_home_scan", auto_publish=auto_publish))
                created.append(task_id); jobs.append(enqueue_auto_youtube_pipeline(task_id, auto_publish=auto_publish))
            except Exception as exc:
                failed += 1; errors.append(f"{item.video_id}: {type(exc).__name__}: {exc}")
        stats = feed.stats
        finish_youtube_home_scan_lock(db, owner=owner, discovered_count=len(feed.videos), started_count=len(jobs), skipped_duplicates=skipped, failed_count=failed, error=(f"{len(errors)} item(s) failed. First error: {errors[0]}" if errors else None), sample_urls=[v.url for v in feed.videos[:5]], candidate_count=stats.candidate_count, explicit_shorts_count=stats.explicit_shorts_count, known_duration_count=stats.known_duration_count, unknown_duration_count=stats.unknown_duration_count, below_min_duration_count=stats.below_min_duration_count, kept_unknown_duration_count=stats.kept_unknown_duration_count, eligible_count=stats.eligible_count, log_lines=list(stats.log_lines))
        acquired = False
        return YouTubeHomeScanRunResponse(discovered_count=len(feed.videos), created_task_ids=created, skipped_duplicates=skipped, failed_count=failed, candidate_count=stats.candidate_count, explicit_shorts_count=stats.explicit_shorts_count, known_duration_count=stats.known_duration_count, unknown_duration_count=stats.unknown_duration_count, below_min_duration_count=stats.below_min_duration_count, kept_unknown_duration_count=stats.kept_unknown_duration_count, eligible_count=stats.eligible_count, min_duration_seconds=stats.min_duration_seconds, log_lines=list(stats.log_lines), started_pipeline_job_ids=jobs, sample_urls=[v.url for v in feed.videos[:5]])
    except Exception as exc:
        if acquired:
            db.rollback(); finish_youtube_home_scan_lock(db, owner=owner, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        db.close()
