from __future__ import annotations

import hashlib
import logging
import mimetypes
import random
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from videoroll.apps.orchestrator_api.services import asset_service, task_service
from videoroll.db.models import AppSetting, Asset, AssetKind, Task
from videoroll.db.session import get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.utils.fernet import decrypt_str, encrypt_str


logger = logging.getLogger(__name__)

LIVE_SETTINGS_KEY = "live.settings"
LIVE_PLAYLIST_KEY = "live.playlist"
LIVE_SESSION_KEY = "live.session"
LIVE_MEDIA_PREFIX = "live.media."
LIVE_MEDIA_MAX_BYTES = 8 * 1024 * 1024 * 1024
LIVE_AUDIO_MAX_BYTES = 2 * 1024 * 1024 * 1024
LIVE_ACTIVE_STATES = frozenset({"starting", "running", "paused"})
LIVE_VIDEO_CONTENT_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"})
LIVE_AUDIO_CONTENT_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/mp4",
        "audio/aac",
        "audio/wav",
        "audio/x-wav",
        "audio/flac",
        "audio/ogg",
        "audio/opus",
        "audio/webm",
    }
)


@dataclass(frozen=True)
class LiveSource:
    source: str
    id: str


@dataclass(frozen=True)
class ResolvedMedia:
    source: LiveSource
    media_type: str
    storage_key: str
    display_name: str


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _row(db: Session, key: str, *, create: bool = False) -> AppSetting | None:
    row = db.get(AppSetting, key)
    if row is None and create:
        row = AppSetting(key=key, value_json={})
        db.add(row)
        db.flush()
    return row


def _safe_name(value: object, fallback: str) -> str:
    name = Path(str(value or "")).name.replace("\x00", "").replace("\r", " ").replace("\n", " ").replace('"', "'").strip()
    return name[:180] if name else fallback


def _normalize_rtmp_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"rtmp", "rtmps"}:
        raise ValueError("推流地址必须使用 rtmp:// 或 rtmps://")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("推流地址必须包含主机，且不能含有用户名或密码")
    if parsed.query:
        raise ValueError("请将授权码填写到推流码字段，不要放入推流地址")
    if raw.endswith("/"):
        raw = raw[:-1]
    sanitized = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return sanitized


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int, field: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _settings_defaults() -> dict[str, Any]:
    return {
        "rtmp_url": "",
        "video_bitrate_kbps": 4500,
        "audio_bitrate_kbps": 160,
        "fps": 30,
        "keyframe_interval_seconds": 2,
    }


def _settings_raw(db: Session) -> dict[str, Any]:
    row = _row(db, LIVE_SETTINGS_KEY)
    return {**_settings_defaults(), **_as_dict(row.value_json if row else None)}


def get_live_settings(db: Session) -> dict[str, Any]:
    stored = _settings_raw(db)
    return {
        "rtmp_url": _normalize_rtmp_url(stored.get("rtmp_url")) if stored.get("rtmp_url") else "",
        "stream_key_set": bool(str(stored.get("stream_key_enc") or "").strip()),
        "video_bitrate_kbps": _bounded_int(stored.get("video_bitrate_kbps"), default=4500, minimum=500, maximum=20000, field="视频码率"),
        "audio_bitrate_kbps": _bounded_int(stored.get("audio_bitrate_kbps"), default=160, minimum=32, maximum=512, field="音频码率"),
        "fps": _bounded_int(stored.get("fps"), default=30, minimum=15, maximum=60, field="帧率"),
        "keyframe_interval_seconds": _bounded_int(
            stored.get("keyframe_interval_seconds"), default=2, minimum=1, maximum=10, field="关键帧间隔"
        ),
    }


def update_live_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _row(db, LIVE_SETTINGS_KEY, create=True)
    if row is None:  # pragma: no cover - appease type checkers
        raise RuntimeError("unable to create live settings")
    stored = _settings_raw(db)

    if "rtmp_url" in update and update["rtmp_url"] is not None:
        stored["rtmp_url"] = _normalize_rtmp_url(update["rtmp_url"])
    for field, default, minimum, maximum, label in (
        ("video_bitrate_kbps", 4500, 500, 20000, "视频码率"),
        ("audio_bitrate_kbps", 160, 32, 512, "音频码率"),
        ("fps", 30, 15, 60, "帧率"),
        ("keyframe_interval_seconds", 2, 1, 10, "关键帧间隔"),
    ):
        if field in update and update[field] is not None:
            stored[field] = _bounded_int(update[field], default=default, minimum=minimum, maximum=maximum, field=label)
    if "stream_key" in update and update["stream_key"] is not None:
        key = str(update["stream_key"] or "").replace("\r", "").replace("\n", "").strip()
        if len(key) > 1024:
            raise ValueError("推流码不能超过 1024 个字符")
        if key:
            stored["stream_key_enc"] = encrypt_str(key)
        else:
            stored.pop("stream_key_enc", None)

    row.value_json = stored
    db.add(row)
    db.commit()
    return get_live_settings(db)


def _stream_target(db: Session) -> tuple[dict[str, Any], str]:
    settings = _settings_raw(db)
    url = _normalize_rtmp_url(settings.get("rtmp_url"))
    token = str(settings.get("stream_key_enc") or "").strip()
    try:
        stream_key = decrypt_str(token).strip()
    except Exception as exc:
        raise ValueError("推流码无法解密，请重新保存") from exc
    if not url:
        raise ValueError("请先填写 RTMP 推流地址")
    if not stream_key:
        raise ValueError("请先填写推流码")
    return get_live_settings(db), f"{url.rstrip('/')}/{stream_key.lstrip('/')}"


def _normalize_source(value: object) -> LiveSource:
    data = _as_dict(value)
    source = str(data.get("source") or "").strip()
    source_id = str(data.get("id") or "").strip()
    if source not in {"library", "task_asset"}:
        raise ValueError("播放列表资源类型无效")
    try:
        source_id = str(uuid.UUID(source_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("播放列表资源 ID 无效") from exc
    return LiveSource(source=source, id=source_id)


def _playlist_defaults() -> dict[str, Any]:
    return {"video_items": [], "audio_items": [], "playback_mode": "sequential", "loop_playlist": True}


def _playlist_raw(db: Session) -> dict[str, Any]:
    row = _row(db, LIVE_PLAYLIST_KEY)
    stored = _as_dict(row.value_json if row else None)
    return {**_playlist_defaults(), **stored}


def get_live_playlist(db: Session) -> dict[str, Any]:
    stored = _playlist_raw(db)
    video_items: list[dict[str, str]] = []
    audio_items: list[dict[str, str]] = []
    for raw in stored.get("video_items") or []:
        try:
            item = _normalize_source(raw)
        except ValueError:
            continue
        video_items.append({"source": item.source, "id": item.id})
    for raw in stored.get("audio_items") or []:
        try:
            item = _normalize_source(raw)
        except ValueError:
            continue
        audio_items.append({"source": item.source, "id": item.id})
    mode = str(stored.get("playback_mode") or "sequential").strip().lower()
    return {
        "video_items": video_items,
        "audio_items": audio_items,
        "playback_mode": mode if mode in {"sequential", "shuffle"} else "sequential",
        "loop_playlist": bool(stored.get("loop_playlist", True)),
    }


def _resolve_media(db: Session, source: LiveSource, *, expected_type: str) -> ResolvedMedia:
    if source.source == "library":
        row = _row(db, f"{LIVE_MEDIA_PREFIX}{source.id}")
        data = _as_dict(row.value_json if row else None)
        if not data:
            raise ValueError("直播媒体库资源不存在")
        if str(data.get("media_type") or "") != expected_type:
            raise ValueError("直播媒体类型不匹配")
        key = str(data.get("storage_key") or "").strip()
        if not key:
            raise ValueError("直播媒体缺少存储对象")
        return ResolvedMedia(source, expected_type, key, _safe_name(data.get("display_name"), "media"))

    asset = db.get(Asset, uuid.UUID(source.id))
    if not asset or asset.kind != AssetKind.video_final:
        raise ValueError("已完成视频不存在或不可用于直播")
    if expected_type != "video":
        raise ValueError("已完成视频不能作为独立音频")
    return ResolvedMedia(source, "video", asset.storage_key, _safe_name(asset.storage_key, "video.mp4"))


def _validate_playlist_sources(db: Session, items: list[LiveSource], *, expected_type: str) -> None:
    if len(items) > 100:
        raise ValueError("每个播放列表最多 100 个资源")
    for item in items:
        _resolve_media(db, item, expected_type=expected_type)


def _existing_completed_video_import(db: Session, asset_id: uuid.UUID) -> LiveSource | None:
    rows = db.query(AppSetting).filter(AppSetting.key.like(f"{LIVE_MEDIA_PREFIX}%")).all()
    for row in rows:
        data = _as_dict(row.value_json)
        if data.get("media_type") != "video" or str(data.get("source_asset_id") or "") != str(asset_id):
            continue
        media_id = str(data.get("id") or row.key.removeprefix(LIVE_MEDIA_PREFIX))
        try:
            return LiveSource(source="library", id=str(uuid.UUID(media_id)))
        except ValueError:
            continue
    return None


def _import_completed_video(
    db: Session,
    s3: S3Store,
    source: LiveSource,
    *,
    copied_keys: list[str],
) -> LiveSource:
    asset_id = uuid.UUID(source.id)
    existing = _existing_completed_video_import(db, asset_id)
    if existing is not None:
        return existing

    asset = db.get(Asset, asset_id)
    if not asset or asset.kind != AssetKind.video_final:
        raise ValueError("已完成视频不存在或不可导入直播媒体库")
    task = db.get(Task, asset.task_id)
    if task is None:
        raise ValueError("已完成视频所属任务不存在")

    extension = Path(asset.storage_key).suffix
    if not extension or len(extension) > 12:
        extension = ".mp4"
    title_map = task_service.load_task_display_titles(db, [task.id], allow_s3_fallback=False)
    display_name = _safe_name(
        f"{str(title_map.get(task.id) or '').strip()}{extension}" if title_map.get(task.id) else Path(asset.storage_key).name,
        f"completed_video{extension}",
    )
    media_id = uuid.uuid4()
    destination_key = f"live/video/{media_id}/imported_{asset.id}{extension.lower()}"
    content_type = str(mimetypes.guess_type(display_name)[0] or "video/mp4")
    size_bytes = int(asset.size_bytes or 0)
    try:
        source_head = s3.head_object(asset.storage_key)
        content_type = str(source_head.get("ContentType") or content_type)
        size_bytes = int(source_head.get("ContentLength") or size_bytes)
    except Exception:
        pass

    s3.copy_object(asset.storage_key, destination_key)
    copied_keys.append(destination_key)
    db.add(
        AppSetting(
            key=f"{LIVE_MEDIA_PREFIX}{media_id}",
            value_json={
                "id": str(media_id),
                "media_type": "video",
                "origin": "completed_video",
                "source_task_id": str(task.id),
                "source_asset_id": str(asset.id),
                "display_name": display_name,
                "storage_key": destination_key,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "sha256": asset.sha256,
                "created_at": _now_iso(),
            },
        )
    )
    return LiveSource(source="library", id=str(media_id))


def update_live_playlist(db: Session, update: dict[str, Any], *, s3: S3Store | None = None) -> dict[str, Any]:
    if _session_status(db).get("status") in LIVE_ACTIVE_STATES:
        raise HTTPException(status_code=409, detail="直播中不能修改播放列表，请先暂停或停止推流")
    current = get_live_playlist(db)
    copied_keys: list[str] = []
    try:
        for field, expected_type in (("video_items", "video"), ("audio_items", "audio")):
            if field not in update or update[field] is None:
                continue
            raw_items = update[field]
            if not isinstance(raw_items, list):
                raise ValueError(f"{field} 必须是数组")
            items = [_normalize_source(raw) for raw in raw_items]
            _validate_playlist_sources(db, items, expected_type=expected_type)
            current[field] = [{"source": item.source, "id": item.id} for item in items]
        if "playback_mode" in update and update["playback_mode"] is not None:
            mode = str(update["playback_mode"] or "").strip().lower()
            if mode not in {"sequential", "shuffle"}:
                raise ValueError("播放模式必须为 sequential 或 shuffle")
            current["playback_mode"] = mode
        if "loop_playlist" in update and update["loop_playlist"] is not None:
            current["loop_playlist"] = bool(update["loop_playlist"])

        video_sources = [_normalize_source(raw) for raw in current["video_items"]]
        if any(item.source == "task_asset" for item in video_sources) and s3 is None:
            raise ValueError("导入已完成视频需要可用的 S3/MinIO 存储")
        materialized_videos = video_sources
        if s3 is not None:
            materialized_videos = [
                _import_completed_video(db, s3, item, copied_keys=copied_keys)
                if item.source == "task_asset"
                else item
                for item in video_sources
            ]
        current["video_items"] = [{"source": item.source, "id": item.id} for item in materialized_videos]

        row = _row(db, LIVE_PLAYLIST_KEY, create=True)
        if row is None:  # pragma: no cover
            raise RuntimeError("unable to create live playlist")
        row.value_json = current
        db.add(row)
        db.commit()
        return get_live_playlist(db)
    except Exception:
        db.rollback()
        for key in copied_keys:
            try:
                asset_service.queue_pending_s3_delete(db, key, reason="failed_live_video_import")
            except Exception:
                db.rollback()
        raise


def _session_defaults() -> dict[str, Any]:
    return {
        "status": "idle",
        "started_at": None,
        "updated_at": None,
        "stopped_at": None,
        "current_video": None,
        "current_audio": None,
        "last_error": None,
    }


def _session_status(db: Session) -> dict[str, Any]:
    row = _row(db, LIVE_SESSION_KEY)
    stored = _as_dict(row.value_json if row else None)
    result = {**_session_defaults(), **stored}
    status = str(result.get("status") or "idle").strip().lower()
    result["status"] = status if status in {"idle", "starting", "running", "paused", "stopped", "failed"} else "failed"
    return result


def get_live_session(db: Session) -> dict[str, Any]:
    return _session_status(db)


def _update_session(db: Session, **patch: Any) -> dict[str, Any]:
    row = _row(db, LIVE_SESSION_KEY, create=True)
    if row is None:  # pragma: no cover
        raise RuntimeError("unable to create live session")
    current = _session_status(db)
    current.update(patch)
    current["updated_at"] = _now_iso()
    row.value_json = current
    db.add(row)
    db.commit()
    return _session_status(db)


def _library_media_row(db: Session, media_id: uuid.UUID | str) -> AppSetting:
    row = _row(db, f"{LIVE_MEDIA_PREFIX}{media_id}")
    if row is None or not _as_dict(row.value_json):
        raise HTTPException(status_code=404, detail="直播媒体不存在")
    return row


def list_live_library_media(db: Session, *, media_type: str | None = None) -> list[dict[str, Any]]:
    rows = (
        db.query(AppSetting)
        .filter(AppSetting.key.like(f"{LIVE_MEDIA_PREFIX}%"))
        .order_by(AppSetting.key.desc())
        .all()
    )
    media: list[dict[str, Any]] = []
    for row in rows:
        data = _as_dict(row.value_json)
        kind = str(data.get("media_type") or "")
        if kind not in {"video", "audio"} or (media_type and kind != media_type):
            continue
        media_id = str(data.get("id") or row.key.removeprefix(LIVE_MEDIA_PREFIX))
        try:
            media_id = str(uuid.UUID(media_id))
        except ValueError:
            continue
        media.append(
            {
                "id": media_id,
                "media_type": kind,
                "display_name": _safe_name(data.get("display_name"), "media"),
                "storage_key": str(data.get("storage_key") or ""),
                "content_type": str(data.get("content_type") or "application/octet-stream"),
                "size_bytes": int(data.get("size_bytes") or 0),
                "sha256": str(data.get("sha256") or "") or None,
                "origin": str(data.get("origin") or "upload"),
                "source_task_id": str(data.get("source_task_id") or "") or None,
                "source_asset_id": str(data.get("source_asset_id") or "") or None,
                "created_at": str(data.get("created_at") or "") or None,
            }
        )
    return media


def list_completed_live_videos(db: Session, *, limit: int = 200) -> list[dict[str, Any]]:
    assets = (
        db.query(Asset)
        .filter(Asset.kind == AssetKind.video_final)
        .order_by(Asset.created_at.desc())
        .limit(max(1, min(int(limit or 1), 500)))
        .all()
    )
    task_ids = [asset.task_id for asset in assets]
    task_map = {task.id: task for task in db.query(Task).filter(Task.id.in_(task_ids)).all()} if task_ids else {}
    title_map = task_service.load_task_display_titles(db, task_ids, allow_s3_fallback=False)
    result: list[dict[str, Any]] = []
    for asset in assets:
        task = task_map.get(asset.task_id)
        if task is None:
            continue
        result.append(
            {
                "id": str(asset.id),
                "task_id": str(task.id),
                "display_name": str(title_map.get(task.id) or "").strip() or _safe_name(asset.storage_key, "video.mp4"),
                "storage_key": asset.storage_key,
                "size_bytes": asset.size_bytes,
                "duration_ms": asset.duration_ms,
                "created_at": asset.created_at,
            }
        )
    return result


def _media_content_type(media_type: str, filename: str, declared: str | None) -> str:
    content_type = str(declared or "").split(";", 1)[0].strip().lower()
    if not content_type or content_type == "application/octet-stream":
        content_type = str(mimetypes.guess_type(filename)[0] or "").lower()
    allowed = LIVE_VIDEO_CONTENT_TYPES if media_type == "video" else LIVE_AUDIO_CONTENT_TYPES
    if content_type not in allowed:
        label = "视频" if media_type == "video" else "音频"
        raise HTTPException(status_code=400, detail=f"上传文件不是受支持的{label}格式")
    return content_type


async def upload_live_media(
    media_type: str,
    file: UploadFile,
    *,
    db: Session,
    s3: S3Store,
) -> dict[str, Any]:
    if media_type not in {"video", "audio"}:
        raise HTTPException(status_code=400, detail="直播媒体类型无效")
    filename = _safe_name(file.filename, "video.mp4" if media_type == "video" else "audio.mp3")
    content_type = _media_content_type(media_type, filename, file.content_type)
    suffix = Path(filename).suffix or (".mp4" if media_type == "video" else ".mp3")
    media_id = uuid.uuid4()
    temp_path: Path | None = None
    storage_key = ""
    try:
        await run_in_threadpool(file.file.seek, 0)
        temp_path, digest, size_bytes = await run_in_threadpool(
            asset_service.stream_upload_to_tempfile,
            file.file,
            prefix=f"videoroll_live_{media_type}_",
            suffix=suffix,
            max_bytes=LIVE_MEDIA_MAX_BYTES if media_type == "video" else LIVE_AUDIO_MAX_BYTES,
        )
        storage_key = f"live/{media_type}/{media_id}/{digest[:16]}_{filename}"
        await run_in_threadpool(s3.upload_file, temp_path, storage_key, content_type)
        row = AppSetting(
            key=f"{LIVE_MEDIA_PREFIX}{media_id}",
            value_json={
                "id": str(media_id),
                "media_type": media_type,
                "origin": "upload",
                "display_name": filename,
                "storage_key": storage_key,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "sha256": digest,
                "created_at": _now_iso(),
            },
        )
        db.add(row)
        db.commit()
        return {
            "id": str(media_id),
            "media_type": media_type,
            "origin": "upload",
            "display_name": filename,
            "storage_key": storage_key,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": digest,
            "created_at": _now_iso(),
        }
    except asset_service.UploadTooLargeError as exc:
        db.rollback()
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        if storage_key:
            try:
                asset_service.queue_pending_s3_delete(db, storage_key, reason="failed_live_media_upload")
            except Exception:
                db.rollback()
        raise HTTPException(status_code=500, detail=f"直播媒体上传失败: {exc}") from exc
    finally:
        await run_in_threadpool(asset_service.safe_unlink, temp_path)
        try:
            await run_in_threadpool(file.file.close)
        except Exception:
            pass


def delete_live_media(media_id: uuid.UUID, *, db: Session) -> dict[str, bool]:
    if _session_status(db).get("status") in LIVE_ACTIVE_STATES:
        raise HTTPException(status_code=409, detail="直播中不能删除媒体")
    playlist = get_live_playlist(db)
    if any(item.get("source") == "library" and item.get("id") == str(media_id) for item in playlist["video_items"] + playlist["audio_items"]):
        raise HTTPException(status_code=409, detail="请先从播放列表移除该媒体")
    row = _library_media_row(db, media_id)
    key = str(_as_dict(row.value_json).get("storage_key") or "").strip()
    if key:
        asset_service.queue_pending_s3_delete(db, key, reason="live_media_deleted", commit=False)
    db.delete(row)
    db.commit()
    return {"deleted": True}


def get_live_media(db: Session, media_id: uuid.UUID) -> dict[str, Any]:
    row = _library_media_row(db, media_id)
    data = _as_dict(row.value_json)
    media_type = str(data.get("media_type") or "")
    if media_type not in {"video", "audio"}:
        raise HTTPException(status_code=404, detail="直播媒体不存在")
    return {
        "id": str(media_id),
        "media_type": media_type,
        "display_name": _safe_name(data.get("display_name"), "media"),
        "storage_key": str(data.get("storage_key") or ""),
        "content_type": str(data.get("content_type") or "application/octet-stream"),
    }


def prepare_live_media_stream(db: Session, s3: S3Store, media_id: uuid.UUID) -> tuple[Any, str, dict[str, str]]:
    media = get_live_media(db, media_id)
    try:
        result = s3.get_object(media["storage_key"])
    except Exception as exc:
        raise HTTPException(status_code=404, detail="直播媒体对象不存在") from exc
    content_type = str(result.get("ContentType") or media["content_type"] or "application/octet-stream")
    return result["Body"], content_type, {"Content-Disposition": f'inline; filename="{media["display_name"]}"', "X-Content-Type-Options": "nosniff"}


def get_live_dashboard(db: Session) -> dict[str, Any]:
    return {
        "settings": get_live_settings(db),
        "session": get_live_session(db),
        "playlist": get_live_playlist(db),
        "library_media": list_live_library_media(db),
        "completed_videos": list_completed_live_videos(db),
    }


def _ffmpeg_command(
    *,
    ffmpeg_path: str,
    video_path: Path,
    audio_path: Path | None,
    config: dict[str, Any],
    target: str,
) -> list[str]:
    fps = int(config["fps"])
    video_bitrate = int(config["video_bitrate_kbps"])
    audio_bitrate = int(config["audio_bitrate_kbps"])
    keyframe = max(1, fps * int(config["keyframe_interval_seconds"]))
    command = [
        str(ffmpeg_path or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-re",
        "-i",
        str(video_path),
    ]
    if audio_path is not None:
        command.extend(["-stream_loop", "-1", "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-shortest"])
    else:
        command.extend(["-map", "0:v:0", "-map", "0:a:0?"])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-g",
            str(keyframe),
            "-b:v",
            f"{video_bitrate}k",
            "-maxrate",
            f"{video_bitrate}k",
            "-bufsize",
            f"{video_bitrate * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_bitrate}k",
            "-ar",
            "44100",
            "-flvflags",
            "no_duration_filesize",
            "-f",
            "flv",
            target,
        ]
    )
    return command


class LiveStreamController:
    """One local FFmpeg session. The application deliberately supports one live output."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[Any] | None = None
        self._paused = False
        self._session_id: str | None = None

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self, settings: Any, *, session_id: str, config: dict[str, Any], target: str, playlist: dict[str, Any]) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise HTTPException(status_code=409, detail="已有直播推流正在运行")
            self._stop_requested.clear()
            self._wake.clear()
            self._paused = False
            self._session_id = session_id
            self._thread = threading.Thread(
                target=self._run,
                args=(settings, session_id, config, target, playlist),
                name="videoroll-live-stream",
                daemon=True,
            )
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                raise HTTPException(status_code=409, detail="当前没有运行中的直播")
            self._paused = True
            process = self._process
        if process and process.poll() is None:
            process.terminate()

    def resume(self) -> None:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                raise HTTPException(status_code=409, detail="当前没有可恢复的直播")
            if not self._paused:
                raise HTTPException(status_code=409, detail="直播未处于暂停状态")
            self._paused = False
            self._wake.set()

    def stop(self) -> None:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return
            self._stop_requested.set()
            self._paused = False
            self._wake.set()
            process = self._process
        if process and process.poll() is None:
            process.terminate()

    def _paused_now(self) -> bool:
        with self._lock:
            return self._paused

    def _set_process(self, process: subprocess.Popen[Any] | None) -> None:
        with self._lock:
            self._process = process

    @staticmethod
    def _persist(settings: Any, **patch: Any) -> None:
        db = get_sessionmaker(settings.database_url)()
        try:
            _update_session(db, **patch)
        except Exception:
            db.rollback()
            logger.exception("failed to persist live stream state")
        finally:
            db.close()

    def _wait_while_paused(self, settings: Any) -> bool:
        announced = False
        while self._paused_now() and not self._stop_requested.is_set():
            if not announced:
                self._persist(settings, status="paused")
                announced = True
            self._wake.wait(timeout=1.0)
            self._wake.clear()
        return not self._stop_requested.is_set()

    @staticmethod
    def _ordered_indices(length: int, mode: str) -> list[int]:
        indices = list(range(length))
        if mode == "shuffle":
            random.shuffle(indices)
        return indices

    def _run(self, settings: Any, session_id: str, config: dict[str, Any], target: str, playlist: dict[str, Any]) -> None:
        work_dir = Path(settings.work_dir) / "live" / session_id
        video_sources = [_normalize_source(item) for item in playlist["video_items"]]
        audio_sources = [_normalize_source(item) for item in playlist["audio_items"]]
        mode = str(playlist.get("playback_mode") or "sequential")
        loop_playlist = bool(playlist.get("loop_playlist", True))
        video_order = self._ordered_indices(len(video_sources), mode)
        audio_order = self._ordered_indices(len(audio_sources), mode) if audio_sources else []
        video_position = 0
        audio_position = 0
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            self._persist(settings, status="running", started_at=_now_iso(), stopped_at=None, last_error=None)
            while not self._stop_requested.is_set():
                if not self._wait_while_paused(settings):
                    break
                if video_position >= len(video_order):
                    if not loop_playlist:
                        break
                    video_order = self._ordered_indices(len(video_sources), mode)
                    video_position = 0
                if audio_sources and audio_position >= len(audio_order):
                    audio_order = self._ordered_indices(len(audio_sources), mode)
                    audio_position = 0

                db = get_sessionmaker(settings.database_url)()
                try:
                    video = _resolve_media(db, video_sources[video_order[video_position]], expected_type="video")
                    audio = _resolve_media(db, audio_sources[audio_order[audio_position]], expected_type="audio") if audio_sources else None
                finally:
                    db.close()

                video_path = work_dir / f"video_{video_position}{Path(video.display_name).suffix or '.mp4'}"
                audio_path = (work_dir / f"audio_{audio_position}{Path(audio.display_name).suffix or '.audio'}") if audio else None
                store = S3Store(settings)
                store.download_file(video.storage_key, video_path)
                if audio and audio_path:
                    store.download_file(audio.storage_key, audio_path)
                self._persist(
                    settings,
                    status="running",
                    current_video={"source": video.source.source, "id": video.source.id, "display_name": video.display_name},
                    current_audio=(
                        {"source": audio.source.source, "id": audio.source.id, "display_name": audio.display_name} if audio else None
                    ),
                )
                log_path = work_dir / "ffmpeg.log"
                command = _ffmpeg_command(
                    ffmpeg_path=settings.ffmpeg_path,
                    video_path=video_path,
                    audio_path=audio_path,
                    config=config,
                    target=target,
                )
                with log_path.open("ab") as log_file:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT)
                    self._set_process(process)
                    return_code = process.wait()
                self._set_process(None)
                if self._stop_requested.is_set():
                    break
                if self._paused_now():
                    continue
                if return_code != 0:
                    raise RuntimeError(f"FFmpeg 推流进程异常退出（exit={return_code}）")
                video_position += 1
                if audio_sources:
                    audio_position += 1
                for path in (video_path, audio_path):
                    if path:
                        path.unlink(missing_ok=True)
            self._persist(settings, status="stopped", stopped_at=_now_iso())
        except Exception as exc:
            logger.exception("live stream failed")
            self._persist(settings, status="failed", stopped_at=_now_iso(), last_error=f"{type(exc).__name__}: {exc}")
        finally:
            self._set_process(None)
            shutil.rmtree(work_dir, ignore_errors=True)
            with self._lock:
                self._thread = None
                self._session_id = None
                self._paused = False
                self._stop_requested.clear()


_CONTROLLER = LiveStreamController()


def get_live_controller() -> LiveStreamController:
    return _CONTROLLER


def recover_interrupted_live_stream(db: Session) -> None:
    """A process restart cannot safely resume an RTMP socket, so close it visibly."""
    session = _session_status(db)
    if session.get("status") in LIVE_ACTIVE_STATES and not _CONTROLLER.is_active():
        _update_session(
            db,
            status="stopped",
            stopped_at=_now_iso(),
            last_error="orchestrator restarted; live stream stopped",
        )


def start_live_stream(settings: Any, *, db: Session) -> dict[str, Any]:
    recover_interrupted_live_stream(db)
    if _CONTROLLER.is_active():
        raise HTTPException(status_code=409, detail="已有直播推流正在运行")
    config, target = _stream_target(db)
    playlist = get_live_playlist(db)
    if any(item.get("source") == "task_asset" for item in playlist["video_items"]):
        playlist = update_live_playlist(db, playlist, s3=S3Store(settings))
    video_items = [_normalize_source(item) for item in playlist["video_items"]]
    if not video_items:
        raise HTTPException(status_code=400, detail="请至少选择一个视频资源")
    _validate_playlist_sources(db, video_items, expected_type="video")
    _validate_playlist_sources(db, [_normalize_source(item) for item in playlist["audio_items"]], expected_type="audio")
    session_id = str(uuid.uuid4())
    _update_session(
        db,
        status="starting",
        session_id=session_id,
        started_at=_now_iso(),
        stopped_at=None,
        current_video=None,
        current_audio=None,
        last_error=None,
    )
    _CONTROLLER.start(settings, session_id=session_id, config=config, target=target, playlist=playlist)
    return get_live_session(db)


def pause_live_stream(*, db: Session) -> dict[str, Any]:
    if _session_status(db).get("status") not in {"starting", "running"}:
        raise HTTPException(status_code=409, detail="直播未处于推流状态")
    _CONTROLLER.pause()
    return _update_session(db, status="paused")


def resume_live_stream(*, db: Session) -> dict[str, Any]:
    if _session_status(db).get("status") != "paused":
        raise HTTPException(status_code=409, detail="直播未处于暂停状态")
    _CONTROLLER.resume()
    return _update_session(db, status="running", last_error=None)


def stop_live_stream(*, db: Session) -> dict[str, Any]:
    _CONTROLLER.stop()
    return _update_session(db, status="stopped", stopped_at=_now_iso())
