from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import yt_dlp
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.youtube_ingest.youtube_feed import fetch_youtube_feed
from videoroll.apps.youtube_settings_store import get_youtube_settings
from videoroll.db.models import (
    IngestedVideo,
    SourceLicense,
    SourceType,
    Task,
    TaskStatus,
    YouTubeSource,
    YouTubeSourceType,
)
from videoroll.utils.auto_youtube import encode_auto_youtube_created_by
from videoroll.utils.youtube_urls import (
    canonicalize_youtube_source_url,
    extract_youtube_source_from_url,
    is_youtube_url,
    normalize_youtube_source_input,
)


logger = logging.getLogger(__name__)

DEFAULT_SOURCE_SCAN_INTERVAL_MINUTES = 60
MIN_SOURCE_SCAN_INTERVAL_MINUTES = 1
MAX_SOURCE_SCAN_INTERVAL_MINUTES = 1440

DEFAULT_SOURCE_SCAN_LIMIT = 20
MIN_SOURCE_SCAN_LIMIT = 1
MAX_SOURCE_SCAN_LIMIT = 200

DEFAULT_SOURCE_SCAN_LOCK_TTL_SECONDS = 900
MAX_SOURCE_SCAN_ERROR_LEN = 1000
MAX_SOURCE_DISPLAY_NAME_LEN = 255


@dataclass(frozen=True)
class ResolvedYouTubeSource:
    source_type: YouTubeSourceType
    source_id: str
    source_url: str
    display_name: str | None = None


@dataclass(frozen=True)
class YouTubeSourceScanResult:
    discovered_count: int
    created_task_ids: list[uuid.UUID]
    skipped_duplicates: int
    started_pipeline_job_ids: list[str]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def normalize_source_scan_interval_minutes(value: Any) -> int:
    try:
        interval = int(value)
    except Exception:
        interval = DEFAULT_SOURCE_SCAN_INTERVAL_MINUTES
    if interval < MIN_SOURCE_SCAN_INTERVAL_MINUTES:
        interval = MIN_SOURCE_SCAN_INTERVAL_MINUTES
    if interval > MAX_SOURCE_SCAN_INTERVAL_MINUTES:
        interval = MAX_SOURCE_SCAN_INTERVAL_MINUTES
    return interval


def normalize_source_scan_limit(value: Any) -> int:
    try:
        limit = int(value)
    except Exception:
        limit = DEFAULT_SOURCE_SCAN_LIMIT
    if limit < MIN_SOURCE_SCAN_LIMIT:
        limit = MIN_SOURCE_SCAN_LIMIT
    if limit > MAX_SOURCE_SCAN_LIMIT:
        limit = MAX_SOURCE_SCAN_LIMIT
    return limit


def _trim_display_name(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > MAX_SOURCE_DISPLAY_NAME_LEN:
        return raw[:MAX_SOURCE_DISPLAY_NAME_LEN]
    return raw


def _trim_error_message(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > MAX_SOURCE_SCAN_ERROR_LEN:
        return raw[: MAX_SOURCE_SCAN_ERROR_LEN - 1] + "…"
    return raw


def _looks_like_playlist_id(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    prefixes = ("PL", "UU", "LL", "FL", "RD", "OLAK5uy_")
    return raw.startswith(prefixes) and len(raw) >= 12


def _looks_like_channel_id(value: str) -> bool:
    raw = str(value or "").strip()
    return raw.startswith("UC") and len(raw) >= 16


def _start_auto_pipeline(task_id: uuid.UUID, *, auto_publish: bool | None = None) -> str:
    from videoroll.apps.subtitle_service.worker import celery_app as subtitle_celery_app

    task_args: list[Any] = [str(task_id)]
    if auto_publish is not None:
        task_args.append({"auto_publish": bool(auto_publish)})
    res = subtitle_celery_app.send_task(
        "subtitle_service.auto_youtube_pipeline",
        args=task_args,
        queue="subtitle",
    )
    return str(res.id)


def _build_resolved_source(
    source_type: str,
    source_id: str,
    *,
    source_url: str | None = None,
    display_name: str | None = None,
) -> ResolvedYouTubeSource:
    kind = YouTubeSourceType(str(source_type or "").strip().lower())
    sid = str(source_id or "").strip()
    return ResolvedYouTubeSource(
        source_type=kind,
        source_id=sid,
        source_url=str(source_url or "").strip() or canonicalize_youtube_source_url(kind.value, sid),
        display_name=_trim_display_name(display_name),
    )


def resolve_youtube_source_input(raw_value: str, user_agent: str, *, proxy: str | None = None) -> ResolvedYouTubeSource:
    raw = normalize_youtube_source_input(raw_value)
    if not raw:
        raise ValueError("source_url is required")

    if _looks_like_channel_id(raw):
        return _build_resolved_source("channel", raw)
    if _looks_like_playlist_id(raw):
        return _build_resolved_source("playlist", raw)

    direct = extract_youtube_source_from_url(raw)
    if direct:
        return _build_resolved_source(direct[0], direct[1])

    if not is_youtube_url(raw):
        raise ValueError("source_url must be a YouTube channel/playlist URL, @handle, or raw UC.../PL... id")

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": 1,
        "retries": 2,
        "fragment_retries": 2,
        "http_headers": {"User-Agent": user_agent},
    }
    if proxy:
        opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(raw, download=False)
    except Exception as e:
        raise ValueError(f"failed to resolve YouTube source: {e}") from e

    data = info if isinstance(info, dict) else {}
    title = _trim_display_name(data.get("channel") or data.get("uploader") or data.get("title"))

    playlist_id = str(data.get("playlist_id") or "").strip()
    webpage_url = str(data.get("webpage_url") or data.get("original_url") or raw).strip()
    if not playlist_id and str(data.get("_type") or "").strip().lower() == "playlist":
        maybe_id = str(data.get("id") or "").strip()
        if maybe_id and (_looks_like_playlist_id(maybe_id) or "list=" in webpage_url or "/playlist" in webpage_url):
            playlist_id = maybe_id
    if playlist_id:
        return _build_resolved_source("playlist", playlist_id, source_url=webpage_url, display_name=title)

    channel_id = str(data.get("channel_id") or "").strip()
    if not channel_id:
        maybe_id = str(data.get("id") or "").strip()
        if _looks_like_channel_id(maybe_id):
            channel_id = maybe_id
    if channel_id:
        channel_url = str(data.get("channel_url") or data.get("uploader_url") or "").strip()
        return _build_resolved_source("channel", channel_id, source_url=channel_url or webpage_url, display_name=title)

    resolved = extract_youtube_source_from_url(webpage_url)
    if resolved:
        return _build_resolved_source(resolved[0], resolved[1], source_url=webpage_url, display_name=title)

    raise ValueError("could not resolve a YouTube channel or playlist from the input")


def youtube_source_is_due(src: YouTubeSource, *, now: datetime | None = None) -> bool:
    if not bool(getattr(src, "enabled", False)):
        return False
    now_dt = now or _utcnow()
    baseline = getattr(src, "last_scan_finished_at", None) or getattr(src, "last_scan_started_at", None)
    if baseline is None:
        return True
    interval = normalize_source_scan_interval_minutes(getattr(src, "scan_interval_minutes", None))
    return now_dt >= baseline + timedelta(minutes=interval)


def youtube_source_scan_is_locked(src: YouTubeSource, *, now: datetime | None = None) -> bool:
    lock_until = getattr(src, "scan_lock_until", None)
    if lock_until is None:
        return False
    return lock_until > (now or _utcnow())


def get_due_youtube_source_ids(db: Session, *, now: datetime | None = None, limit: int | None = None) -> list[uuid.UUID]:
    now_dt = now or _utcnow()
    rows = db.query(YouTubeSource).filter(YouTubeSource.enabled.is_(True)).all()
    due_rows = [row for row in rows if youtube_source_is_due(row, now=now_dt)]
    due_rows.sort(key=lambda row: getattr(row, "last_scan_finished_at", None) or getattr(row, "last_scan_started_at", None) or row.created_at)
    ids = [row.id for row in due_rows]
    if limit is not None:
        return ids[: max(0, int(limit))]
    return ids


def try_acquire_youtube_source_scan_lock(
    db: Session,
    source_pk: uuid.UUID,
    *,
    owner: str,
    ttl_seconds: int,
) -> YouTubeSource | None:
    row = db.execute(select(YouTubeSource).where(YouTubeSource.id == source_pk).with_for_update()).scalar_one_or_none()
    if row is None:
        return None

    now_dt = _utcnow()
    if row.scan_lock_until is not None and row.scan_lock_until > now_dt:
        return None

    row.scan_lock_owner = str(owner or "").strip() or None
    row.scan_lock_until = now_dt + timedelta(seconds=max(60, int(ttl_seconds or DEFAULT_SOURCE_SCAN_LOCK_TTL_SECONDS)))
    row.last_scan_started_at = now_dt
    row.last_scan_error = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def finish_youtube_source_scan_lock(
    db: Session,
    source_pk: uuid.UUID,
    *,
    owner: str,
    discovered_count: int = 0,
    created_count: int = 0,
    started_pipeline_count: int = 0,
    skipped_duplicates: int = 0,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> YouTubeSource | None:
    row = db.execute(select(YouTubeSource).where(YouTubeSource.id == source_pk).with_for_update()).scalar_one_or_none()
    if row is None:
        return None
    current_owner = str(row.scan_lock_owner or "").strip()
    if current_owner and current_owner != str(owner or "").strip():
        return row

    row.scan_lock_owner = None
    row.scan_lock_until = None
    row.last_scan_finished_at = finished_at or _utcnow()
    row.last_scan_discovered_count = max(0, int(discovered_count or 0))
    row.last_scan_created_count = max(0, int(created_count or 0))
    row.last_scan_started_pipeline_count = max(0, int(started_pipeline_count or 0))
    row.last_scan_skipped_duplicates = max(0, int(skipped_duplicates or 0))
    row.last_scan_error = _trim_error_message(error)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def source_to_read_dict(src: YouTubeSource) -> dict[str, Any]:
    source_url = str(getattr(src, "source_url", None) or "").strip() or canonicalize_youtube_source_url(src.source_type.value, src.source_id)
    enabled = getattr(src, "enabled", None)
    auto_process = getattr(src, "auto_process", None)
    return {
        "id": src.id,
        "source_type": src.source_type,
        "source_id": src.source_id,
        "source_url": source_url,
        "display_name": _trim_display_name(getattr(src, "display_name", None)),
        "license": src.license,
        "proof_url": src.proof_url,
        "enabled": True if enabled is None else bool(enabled),
        "scan_interval_minutes": normalize_source_scan_interval_minutes(getattr(src, "scan_interval_minutes", None)),
        "scan_limit": normalize_source_scan_limit(getattr(src, "scan_limit", None)),
        "auto_process": True if auto_process is None else bool(auto_process),
        "last_scan_started_at": getattr(src, "last_scan_started_at", None),
        "last_scan_finished_at": getattr(src, "last_scan_finished_at", None),
        "last_scan_discovered_count": max(0, int(getattr(src, "last_scan_discovered_count", 0) or 0)),
        "last_scan_created_count": max(0, int(getattr(src, "last_scan_created_count", 0) or 0)),
        "last_scan_started_pipeline_count": max(0, int(getattr(src, "last_scan_started_pipeline_count", 0) or 0)),
        "last_scan_skipped_duplicates": max(0, int(getattr(src, "last_scan_skipped_duplicates", 0) or 0)),
        "last_scan_error": _trim_error_message(getattr(src, "last_scan_error", None)),
        "created_at": src.created_at,
        "updated_at": src.updated_at,
    }


def upsert_youtube_source(
    db: Session,
    *,
    source_input: str | None,
    source_type: YouTubeSourceType | str | None,
    source_id: str | None,
    license: SourceLicense,
    proof_url: str | None,
    enabled: bool,
    scan_interval_minutes: int,
    scan_limit: int,
    auto_process: bool,
    user_agent: str,
    default_proxy: str | None = None,
) -> YouTubeSource:
    yt_cfg = get_youtube_settings(db, default_proxy=default_proxy)
    proxy = str(yt_cfg.get("proxy") or "").strip() or default_proxy or None

    if str(source_input or "").strip():
        resolved = resolve_youtube_source_input(str(source_input or ""), user_agent, proxy=proxy)
    else:
        raw_type = str(source_type or "").strip().lower()
        raw_id = str(source_id or "").strip()
        if raw_type not in {"channel", "playlist"} or not raw_id:
            raise ValueError("source_type/source_id or source_url is required")
        resolved = _build_resolved_source(raw_type, raw_id)

    src = (
        db.query(YouTubeSource)
        .filter(YouTubeSource.source_type == resolved.source_type, YouTubeSource.source_id == resolved.source_id)
        .first()
    )
    if src is None:
        src = YouTubeSource(
            source_type=resolved.source_type,
            source_id=resolved.source_id,
        )
        db.add(src)

    src.source_url = resolved.source_url
    if resolved.display_name:
        src.display_name = resolved.display_name
    src.license = license
    src.proof_url = str(proof_url or "").strip() or None
    src.enabled = bool(enabled)
    src.scan_interval_minutes = normalize_source_scan_interval_minutes(scan_interval_minutes)
    src.scan_limit = normalize_source_scan_limit(scan_limit)
    src.auto_process = bool(auto_process)

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise ValueError(f"create source failed: {e}") from e
    db.refresh(src)
    return src


def update_youtube_source(db: Session, source_pk: uuid.UUID, updates: dict[str, Any]) -> YouTubeSource:
    src = db.get(YouTubeSource, source_pk)
    if src is None:
        raise KeyError("source not found")

    if "license" in updates and updates["license"] is not None:
        src.license = updates["license"]
    if "proof_url" in updates:
        src.proof_url = str(updates.get("proof_url") or "").strip() or None
    if "enabled" in updates and updates["enabled"] is not None:
        src.enabled = bool(updates["enabled"])
    if "scan_interval_minutes" in updates and updates["scan_interval_minutes"] is not None:
        src.scan_interval_minutes = normalize_source_scan_interval_minutes(updates["scan_interval_minutes"])
    if "scan_limit" in updates and updates["scan_limit"] is not None:
        src.scan_limit = normalize_source_scan_limit(updates["scan_limit"])
    if "auto_process" in updates and updates["auto_process"] is not None:
        src.auto_process = bool(updates["auto_process"])
    if "display_name" in updates:
        src.display_name = _trim_display_name(updates.get("display_name"))

    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def _prepare_scan_entries(
    entries: list[Any],
    *,
    existing_video_ids: set[str],
    create_limit: int,
    since: datetime | None = None,
) -> tuple[list[Any], list[Any], int]:
    unique_entries: list[Any] = []
    seen_video_ids: set[str] = set()

    for entry in entries:
        video_id = str(getattr(entry, "video_id", "") or "").strip()
        if not video_id or video_id in seen_video_ids:
            continue
        published_at = getattr(entry, "published_at", None)
        if since is not None and published_at is not None and published_at <= since:
            continue
        seen_video_ids.add(video_id)
        unique_entries.append(entry)

    pending_entries = [entry for entry in unique_entries if str(getattr(entry, "video_id", "") or "").strip() not in existing_video_ids]
    skipped_duplicates = len(unique_entries) - len(pending_entries)
    safe_limit = max(0, int(create_limit or 0))
    return unique_entries, pending_entries[:safe_limit], skipped_duplicates


def scan_youtube_source_by_id(
    db: Session,
    source_pk: uuid.UUID,
    *,
    user_agent: str,
    default_proxy: str | None = None,
    limit_override: int | None = None,
    auto_process_override: bool | None = None,
    since: datetime | None = None,
    force: bool = False,
    raise_if_locked: bool = False,
    lock_owner_prefix: str = "youtube_source_scan",
    lock_ttl_seconds: int = DEFAULT_SOURCE_SCAN_LOCK_TTL_SECONDS,
) -> YouTubeSourceScanResult | None:
    src = db.get(YouTubeSource, source_pk)
    if src is None:
        raise KeyError("source not found")
    if not bool(src.enabled):
        raise ValueError("source not found or disabled")
    if not force and not youtube_source_is_due(src):
        return None

    owner = f"{str(lock_owner_prefix or 'youtube_source_scan').strip()}:{uuid.uuid4().hex[:8]}"
    locked = try_acquire_youtube_source_scan_lock(
        db,
        source_pk,
        owner=owner,
        ttl_seconds=lock_ttl_seconds,
    )
    if locked is None:
        if raise_if_locked:
            raise RuntimeError("youtube source scan is already running")
        return None

    limit = normalize_source_scan_limit(limit_override if limit_override is not None else getattr(locked, "scan_limit", None))
    auto_process = bool(getattr(locked, "auto_process", True)) if auto_process_override is None else bool(auto_process_override)
    auto_publish = bool(get_auto_profile(db).get("auto_publish")) if auto_process else None
    yt_cfg = get_youtube_settings(db, default_proxy=default_proxy)
    proxy = str(yt_cfg.get("proxy") or "").strip() or default_proxy or None

    created: list[uuid.UUID] = []
    started: list[str] = []
    skipped = 0
    error_message: str | None = None

    try:
        try:
            entries = list(
                fetch_youtube_feed(
                    locked.source_type.value,
                    locked.source_id,
                    user_agent=user_agent,
                    proxy=proxy,
                    limit=None,
                )
            )
        except Exception as e:
            raise RuntimeError(f"fetch youtube feed failed: {e}") from e

        existing_video_ids: set[str] = set()
        entry_video_ids = [entry.video_id for entry in entries if str(getattr(entry, "video_id", "") or "").strip()]
        if entry_video_ids:
            existing_rows = (
                db.query(IngestedVideo.source_id)
                .filter(IngestedVideo.platform == "youtube", IngestedVideo.source_id.in_(entry_video_ids))
                .all()
            )
            existing_video_ids = {str(row[0]).strip() for row in existing_rows if row and row[0]}

        entries, selected_entries, skipped = _prepare_scan_entries(
            entries,
            existing_video_ids=existing_video_ids,
            create_limit=limit,
            since=since,
        )

        for entry in selected_entries:
            task = Task(
                source_type=SourceType.youtube,
                source_url=f"https://www.youtube.com/watch?v={entry.video_id}",
                source_license=locked.license,
                source_proof_url=locked.proof_url,
                status=TaskStatus.ingested,
                created_by=encode_auto_youtube_created_by("auto_youtube", auto_publish=auto_publish) if auto_process else None,
            )
            db.add(task)
            db.flush()
            db.add(
                IngestedVideo(
                    platform="youtube",
                    source_id=entry.video_id,
                    task_id=task.id,
                    published_at=entry.published_at,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if (
                    db.query(IngestedVideo)
                    .filter(IngestedVideo.platform == "youtube", IngestedVideo.source_id == entry.video_id)
                    .first()
                    is not None
                ):
                    skipped += 1
                    continue
                raise

            db.refresh(task)
            created.append(task.id)
            if auto_process:
                try:
                    started.append(_start_auto_pipeline(task.id, auto_publish=auto_publish))
                except Exception as e:
                    error_message = _trim_error_message(f"{entry.video_id}: start pipeline failed: {e}")
                    logger.exception("failed to start auto pipeline for youtube source task %s", task.id)

        finish_youtube_source_scan_lock(
            db,
            source_pk,
            owner=owner,
            discovered_count=len(entries),
            created_count=len(created),
            started_pipeline_count=len(started),
            skipped_duplicates=skipped,
            error=error_message,
        )
        return YouTubeSourceScanResult(
            discovered_count=len(entries),
            created_task_ids=created,
            skipped_duplicates=skipped,
            started_pipeline_job_ids=started,
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        finish_youtube_source_scan_lock(
            db,
            source_pk,
            owner=owner,
            discovered_count=0,
            created_count=len(created),
            started_pipeline_count=len(started),
            skipped_duplicates=skipped,
            error=_trim_error_message(str(e)),
        )
        raise
