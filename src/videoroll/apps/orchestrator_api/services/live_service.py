from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import random
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yt_dlp
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_header_token
from videoroll.apps.orchestrator_api.services import asset_service, task_service
from videoroll.apps.orchestrator_api.youtube_downloader import build_ydl_opts
from videoroll.apps.egress_gateway.client import resolve_public_endpoint
from videoroll.apps.security.service_auth import INTERNAL_TOKEN_HEADER
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.config import get_subtitle_settings
from videoroll.db.models import AppSetting, Asset, AssetKind, Task
from videoroll.db.session import get_sessionmaker
from videoroll.storage.filesystem import FileStore
from videoroll.utils.fernet import decrypt_str, encrypt_str


logger = logging.getLogger(__name__)

LIVE_SETTINGS_KEY = "live.settings"
LIVE_PLAYLIST_KEY = "live.playlist"
LIVE_SESSION_KEY = "live.session"
LIVE_MEDIA_PREFIX = "live.media."
LIVE_INPUT_SOURCE_PREFIX = "live.input_source."
LIVE_AUDIO_PLAYLIST_PREFIX = "live.audio_playlist."
LIVE_MEDIA_MAX_BYTES = 8 * 1024 * 1024 * 1024
LIVE_AUDIO_MAX_BYTES = 2 * 1024 * 1024 * 1024
LIVE_ACTIVE_STATES = frozenset({"starting", "running", "paused"})
LIVE_MIXER_WIDTH = 1280
LIVE_MIXER_HEIGHT = 720
LIVE_MIXER_AUDIO_RATE = 44100
LIVE_PREVIEW_DIRECTORY = "preview"
LIVE_PREVIEW_PLAYLIST = "stream.m3u8"
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
    is_live_source: bool = False


@dataclass(frozen=True)
class ResolvedLiveInput:
    url: str
    http_headers: dict[str, str]
    has_audio: bool


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


def _normalize_live_input_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("请填写直播源地址")
    if len(raw) > 4096:
        raise ValueError("直播源地址过长")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("直播源地址端口无效") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("直播源地址必须使用 http:// 或 https://")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("直播源地址必须包含主机，且不能包含用户名或密码")
    if port is not None and port not in {80, 443}:
        raise ValueError("直播源地址仅支持标准 HTTP/HTTPS 端口")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("直播源地址不能指向本机或私有网络")
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("直播源地址不能指向本机或私有网络")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def _live_input_display_name(value: object, url: str) -> str:
    explicit = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
    if explicit:
        return explicit[:180]
    parsed = urlsplit(url)
    path_name = Path(parsed.path).name
    fallback = f"{parsed.hostname or 'live'}{f' / {path_name}' if path_name else ''}"
    return fallback[:180]


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
    if source not in {"library", "task_asset", "live_source"}:
        raise ValueError("播放列表资源类型无效")
    try:
        source_id = str(uuid.UUID(source_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("播放列表资源 ID 无效") from exc
    return LiveSource(source=source, id=source_id)


def _playlist_defaults() -> dict[str, Any]:
    return {
        "video_items": [],
        "audio_items": [],
        "playback_mode": "sequential",
        "audio_playback_mode": "sequential",
        "loop_playlist": True,
        "mix_audio": False,
    }


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
    audio_mode = str(stored.get("audio_playback_mode") or "sequential").strip().lower()
    return {
        "video_items": video_items,
        "audio_items": audio_items,
        "playback_mode": mode if mode in {"sequential", "shuffle"} else "sequential",
        "audio_playback_mode": audio_mode if audio_mode in {"sequential", "shuffle"} else "sequential",
        "loop_playlist": bool(stored.get("loop_playlist", True)),
        "mix_audio": bool(stored.get("mix_audio", False)),
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

    if source.source == "live_source":
        row = _row(db, f"{LIVE_INPUT_SOURCE_PREFIX}{source.id}")
        data = _as_dict(row.value_json if row else None)
        if not data:
            raise ValueError("直播源不存在")
        if expected_type != "video":
            raise ValueError("直播源不能作为独立音频")
        url = _normalize_live_input_url(data.get("url"))
        return ResolvedMedia(
            source,
            "video",
            url,
            _live_input_display_name(data.get("display_name"), url),
            is_live_source=True,
        )

    asset = db.get(Asset, uuid.UUID(source.id))
    if not asset or asset.kind not in {AssetKind.video_raw, AssetKind.video_final}:
        raise ValueError("任务视频不存在或不可用于直播")
    if expected_type != "video":
        raise ValueError("任务视频不能作为独立音频")
    return ResolvedMedia(source, "video", asset.storage_key, _safe_name(asset.storage_key, "video.mp4"))


def _validate_playlist_sources(db: Session, items: list[LiveSource], *, expected_type: str) -> None:
    if len(items) > 100:
        raise ValueError("每个播放列表最多 100 个资源")
    for item in items:
        _resolve_media(db, item, expected_type=expected_type)


def _existing_task_video_import(db: Session, asset_id: uuid.UUID) -> LiveSource | None:
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


def _import_task_video(
    db: Session,
    s3: FileStore,
    source: LiveSource,
    *,
    copied_keys: list[str],
) -> LiveSource:
    asset_id = uuid.UUID(source.id)
    existing = _existing_task_video_import(db, asset_id)
    if existing is not None:
        return existing

    asset = db.get(Asset, asset_id)
    if not asset or asset.kind not in {AssetKind.video_raw, AssetKind.video_final}:
        raise ValueError("任务视频不存在或不可导入直播媒体库")
    task = db.get(Task, asset.task_id)
    if task is None:
        raise ValueError("任务视频所属任务不存在")

    extension = Path(asset.storage_key).suffix
    if not extension or len(extension) > 12:
        extension = ".mp4"
    title_map = task_service.load_task_display_titles(db, [task.id], allow_s3_fallback=False)
    display_name = _safe_name(
        f"{str(title_map.get(task.id) or '').strip()}{extension}" if title_map.get(task.id) else Path(asset.storage_key).name,
        f"task_video{extension}",
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
                "origin": "raw_video" if asset.kind == AssetKind.video_raw else "completed_video",
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


def import_task_videos(asset_ids: list[uuid.UUID], *, db: Session, s3: FileStore) -> list[dict[str, Any]]:
    if _session_status(db).get("status") in LIVE_ACTIVE_STATES:
        raise HTTPException(status_code=409, detail="直播中不能导入媒体资源")
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    if not unique_asset_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个任务视频")
    if len(unique_asset_ids) > 100:
        raise HTTPException(status_code=400, detail="每次最多导入 100 个任务视频")

    copied_keys: list[str] = []
    try:
        sources = [
            _import_task_video(
                db,
                s3,
                LiveSource(source="task_asset", id=str(asset_id)),
                copied_keys=copied_keys,
            )
            for asset_id in unique_asset_ids
        ]
        db.commit()
    except Exception as exc:
        db.rollback()
        for key in copied_keys:
            try:
                asset_service.queue_pending_s3_delete(db, key, reason="failed_live_video_import")
            except Exception:
                db.rollback()
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise

    media_by_id = {item["id"]: item for item in list_live_library_media(db, media_type="video")}
    return [media_by_id[source.id] for source in sources if source.id in media_by_id]


def update_live_playlist(db: Session, update: dict[str, Any], *, s3: FileStore | None = None) -> dict[str, Any]:
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
        if "audio_playback_mode" in update and update["audio_playback_mode"] is not None:
            mode = str(update["audio_playback_mode"] or "").strip().lower()
            if mode not in {"sequential", "shuffle"}:
                raise ValueError("音频播放模式必须为 sequential 或 shuffle")
            current["audio_playback_mode"] = mode
        if "loop_playlist" in update and update["loop_playlist"] is not None:
            current["loop_playlist"] = bool(update["loop_playlist"])
        if "mix_audio" in update and update["mix_audio"] is not None:
            current["mix_audio"] = bool(update["mix_audio"])

        video_sources = [_normalize_source(raw) for raw in current["video_items"]]
        if any(item.source == "task_asset" for item in video_sources) and s3 is None:
            raise ValueError("导入任务视频需要可用的共享文件存储")
        materialized_videos = video_sources
        if s3 is not None:
            materialized_videos = [
                _import_task_video(db, s3, item, copied_keys=copied_keys)
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
        "audio_player_status": "idle",
        "audio_position_seconds": 0.0,
        "audio_duration_seconds": None,
        "audio_playback_mode": "sequential",
        "audio_volume_percent": 100,
        "mix_audio": False,
        "last_error": None,
    }


def _session_status(db: Session) -> dict[str, Any]:
    row = _row(db, LIVE_SESSION_KEY)
    stored = _as_dict(row.value_json if row else None)
    result = {**_session_defaults(), **stored}
    status = str(result.get("status") or "idle").strip().lower()
    result["status"] = status if status in {"idle", "starting", "running", "paused", "stopped", "failed"} else "failed"
    audio_mode = str(result.get("audio_playback_mode") or "sequential").strip().lower()
    result["audio_playback_mode"] = audio_mode if audio_mode in {"sequential", "shuffle"} else "sequential"
    result["audio_volume_percent"] = _bounded_int(
        result.get("audio_volume_percent"),
        default=100,
        minimum=0,
        maximum=200,
        field="独立音频音量",
    )
    return result


def get_live_session(db: Session) -> dict[str, Any]:
    result = _session_status(db)
    controller = globals().get("_CONTROLLER")
    if isinstance(controller, LiveStreamController) and controller.is_active():
        result.update(controller.audio_playback_state())
    return result


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


def _live_audio_playlist_row(db: Session, playlist_id: uuid.UUID | str) -> AppSetting:
    row = _row(db, f"{LIVE_AUDIO_PLAYLIST_PREFIX}{playlist_id}")
    if row is None or not _as_dict(row.value_json):
        raise HTTPException(status_code=404, detail="直播歌单不存在")
    return row


def _normalize_live_audio_playlist_name(value: object) -> str:
    name = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
    if not name:
        raise ValueError("请填写歌单名称")
    return name[:180]


def _audio_playlist_media_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        try:
            media_id = str(uuid.UUID(str(raw)))
        except (TypeError, ValueError, AttributeError):
            continue
        if media_id not in result:
            result.append(media_id)
    return result


def _live_audio_playlist_data(row: AppSetting) -> dict[str, Any]:
    data = _as_dict(row.value_json)
    playlist_id = str(data.get("id") or row.key.removeprefix(LIVE_AUDIO_PLAYLIST_PREFIX))
    return {
        "id": str(uuid.UUID(playlist_id)),
        "display_name": _normalize_live_audio_playlist_name(data.get("display_name")),
        "audio_media_ids": _audio_playlist_media_ids(data.get("audio_media_ids")),
        "created_at": str(data.get("created_at") or "") or None,
        "updated_at": str(data.get("updated_at") or "") or None,
    }


def list_live_audio_playlists(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(AppSetting)
        .filter(AppSetting.key.like(f"{LIVE_AUDIO_PLAYLIST_PREFIX}%"))
        .order_by(AppSetting.key.desc())
        .all()
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            result.append(_live_audio_playlist_data(row))
        except (TypeError, ValueError):
            continue
    return result


def create_live_audio_playlist(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    playlist_id = uuid.uuid4()
    now = _now_iso()
    row = AppSetting(
        key=f"{LIVE_AUDIO_PLAYLIST_PREFIX}{playlist_id}",
        value_json={
            "id": str(playlist_id),
            "display_name": _normalize_live_audio_playlist_name(payload.get("display_name")),
            "audio_media_ids": [],
            "created_at": now,
            "updated_at": now,
        },
    )
    db.add(row)
    db.commit()
    return _live_audio_playlist_data(row)


def _add_audio_to_live_audio_playlist(db: Session, playlist_id: uuid.UUID, media_id: uuid.UUID) -> None:
    row = _live_audio_playlist_row(db, playlist_id)
    current = _as_dict(row.value_json)
    media_ids = _audio_playlist_media_ids(current.get("audio_media_ids"))
    if str(media_id) not in media_ids:
        media_ids.append(str(media_id))
    row.value_json = {**current, "id": str(playlist_id), "audio_media_ids": media_ids, "updated_at": _now_iso()}
    db.add(row)


def _remove_audio_from_live_audio_playlists(db: Session, media_id: uuid.UUID) -> None:
    for row in db.query(AppSetting).filter(AppSetting.key.like(f"{LIVE_AUDIO_PLAYLIST_PREFIX}%")).all():
        current = _as_dict(row.value_json)
        media_ids = _audio_playlist_media_ids(current.get("audio_media_ids"))
        if str(media_id) not in media_ids:
            continue
        row.value_json = {
            **current,
            "audio_media_ids": [item for item in media_ids if item != str(media_id)],
            "updated_at": _now_iso(),
        }
        db.add(row)


def delete_live_audio_playlist(playlist_id: uuid.UUID, *, db: Session) -> dict[str, bool]:
    row = _live_audio_playlist_row(db, playlist_id)
    db.delete(row)
    db.commit()
    return {"deleted": True}


def _live_input_source_row(db: Session, source_id: uuid.UUID | str) -> AppSetting:
    row = _row(db, f"{LIVE_INPUT_SOURCE_PREFIX}{source_id}")
    if row is None or not _as_dict(row.value_json):
        raise HTTPException(status_code=404, detail="直播源不存在")
    return row


def _live_input_source_data(row: AppSetting) -> dict[str, Any]:
    data = _as_dict(row.value_json)
    source_id = str(data.get("id") or row.key.removeprefix(LIVE_INPUT_SOURCE_PREFIX))
    source_uuid = uuid.UUID(source_id)
    url = _normalize_live_input_url(data.get("url"))
    return {
        "id": str(source_uuid),
        "url": url,
        "display_name": _live_input_display_name(data.get("display_name"), url),
        "created_at": str(data.get("created_at") or "") or None,
    }


def list_live_input_sources(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(AppSetting)
        .filter(AppSetting.key.like(f"{LIVE_INPUT_SOURCE_PREFIX}%"))
        .order_by(AppSetting.key.desc())
        .all()
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            result.append(_live_input_source_data(row))
        except (TypeError, ValueError):
            continue
    return result


def create_live_input_source(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    url = _normalize_live_input_url(payload.get("url"))
    source_id = uuid.uuid4()
    row = AppSetting(
        key=f"{LIVE_INPUT_SOURCE_PREFIX}{source_id}",
        value_json={
            "id": str(source_id),
            "url": url,
            "display_name": _live_input_display_name(payload.get("display_name"), url),
            "created_at": _now_iso(),
        },
    )
    db.add(row)
    db.commit()
    return _live_input_source_data(row)


def update_live_input_source(source_id: uuid.UUID, payload: dict[str, Any], *, db: Session) -> dict[str, Any]:
    if _session_status(db).get("status") in LIVE_ACTIVE_STATES:
        raise HTTPException(status_code=409, detail="直播中不能修改直播源")
    row = _live_input_source_row(db, source_id)
    current = _as_dict(row.value_json)
    url = _normalize_live_input_url(payload.get("url") if "url" in payload else current.get("url"))
    display_value = payload.get("display_name") if "display_name" in payload else current.get("display_name")
    row.value_json = {
        **current,
        "id": str(source_id),
        "url": url,
        "display_name": _live_input_display_name(display_value, url),
    }
    db.add(row)
    db.commit()
    return _live_input_source_data(row)


def delete_live_input_source(source_id: uuid.UUID, *, db: Session) -> dict[str, bool]:
    if _session_status(db).get("status") in LIVE_ACTIVE_STATES:
        raise HTTPException(status_code=409, detail="直播中不能删除直播源")
    playlist = get_live_playlist(db)
    if any(item.get("source") == "live_source" and item.get("id") == str(source_id) for item in playlist["video_items"]):
        raise HTTPException(status_code=409, detail="请先从播放列表移除该直播源")
    row = _live_input_source_row(db, source_id)
    db.delete(row)
    db.commit()
    return {"deleted": True}


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
        media.append(_live_media_data(row, media_id=media_id, data=data, media_type=kind))
    return media


def _live_media_data(row: AppSetting, *, media_id: str | None = None, data: dict[str, Any] | None = None, media_type: str | None = None) -> dict[str, Any]:
    stored = data if data is not None else _as_dict(row.value_json)
    resolved_id = media_id or str(stored.get("id") or row.key.removeprefix(LIVE_MEDIA_PREFIX))
    kind = media_type or str(stored.get("media_type") or "")
    return {
        "id": resolved_id,
        "media_type": kind,
        "display_name": _safe_name(stored.get("display_name"), "media"),
        "storage_key": str(stored.get("storage_key") or ""),
        "content_type": str(stored.get("content_type") or "application/octet-stream"),
        "size_bytes": int(stored.get("size_bytes") or 0),
        "sha256": str(stored.get("sha256") or "") or None,
        "origin": str(stored.get("origin") or "upload"),
        "source_task_id": str(stored.get("source_task_id") or "") or None,
        "source_asset_id": str(stored.get("source_asset_id") or "") or None,
        "created_at": str(stored.get("created_at") or "") or None,
    }


def _list_task_live_videos(db: Session, kind: AssetKind, *, limit: int = 200) -> list[dict[str, Any]]:
    assets = (
        db.query(Asset)
        .filter(Asset.kind == kind)
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


def list_completed_live_videos(db: Session, *, limit: int = 200) -> list[dict[str, Any]]:
    return _list_task_live_videos(db, AssetKind.video_final, limit=limit)


def list_raw_live_videos(db: Session, *, limit: int = 200) -> list[dict[str, Any]]:
    return _list_task_live_videos(db, AssetKind.video_raw, limit=limit)


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
    s3: FileStore,
    audio_playlist_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    if media_type not in {"video", "audio"}:
        raise HTTPException(status_code=400, detail="直播媒体类型无效")
    if audio_playlist_id is not None:
        if media_type != "audio":
            raise HTTPException(status_code=400, detail="只有音频可以加入歌单")
        _live_audio_playlist_row(db, audio_playlist_id)
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
            suffix=f"{suffix}.partial" if isinstance(s3, FileStore) else suffix,
            max_bytes=LIVE_MEDIA_MAX_BYTES if media_type == "video" else LIVE_AUDIO_MAX_BYTES,
            directory=s3.partial_root if isinstance(s3, FileStore) else None,
        )
        storage_key = f"live/{media_type}/{media_id}/{digest[:16]}_{filename}"
        if isinstance(s3, FileStore):
            await run_in_threadpool(s3.promote_file, temp_path, storage_key)
        else:
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
        if audio_playlist_id is not None:
            _add_audio_to_live_audio_playlist(db, audio_playlist_id, media_id)
        db.commit()
        return _live_media_data(row)
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
    if _as_dict(row.value_json).get("media_type") == "audio":
        _remove_audio_from_live_audio_playlists(db, media_id)
    if key:
        asset_service.queue_pending_s3_delete(db, key, reason="live_media_deleted", commit=False)
    db.delete(row)
    db.commit()
    return {"deleted": True}


def rename_live_video_media(media_id: uuid.UUID, display_name: object, *, db: Session) -> dict[str, Any]:
    row = _library_media_row(db, media_id)
    current = _as_dict(row.value_json)
    if str(current.get("media_type") or "") != "video":
        raise HTTPException(status_code=400, detail="只能修改视频资源名称")
    if not str(display_name or "").strip():
        raise HTTPException(status_code=400, detail="请填写视频名称")
    renamed = _safe_name(display_name, "video")
    row.value_json = {**current, "id": str(media_id), "display_name": renamed}
    db.add(row)
    db.commit()
    return _live_media_data(row)


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


def prepare_live_media_stream(
    db: Session,
    s3: FileStore,
    media_id: uuid.UUID,
    *,
    range_header: str = "",
) -> asset_service.AssetStreamResult:
    media = get_live_media(db, media_id)
    total_size: int | None = None
    stored_content_type = str(media["content_type"] or "application/octet-stream")
    try:
        head = s3.head_object(media["storage_key"])
        if isinstance(head.get("ContentLength"), int):
            total_size = int(head["ContentLength"])
        if head.get("ContentType"):
            stored_content_type = str(head["ContentType"] or stored_content_type)
    except Exception as exc:
        logger.warning("unable to read live media metadata %s: %s", media_id, type(exc).__name__)

    base_headers = {
        "Accept-Ranges": "bytes",
        # Response headers are latin-1 at the ASGI boundary.  Live media names
        # may contain CJK characters, so use the shared RFC 5987-safe builder
        # instead of placing the display name directly in ``filename``.
        "Content-Disposition": asset_service.content_disposition(media["display_name"], inline=True),
        "X-Content-Type-Options": "nosniff",
    }
    if range_header and isinstance(total_size, int) and total_size > 0:
        parsed_range = asset_service.parse_range_header(range_header, total_size)
        if not parsed_range:
            return asset_service.AssetStreamResult(
                body=None,
                media_type=None,
                headers={**base_headers, "Content-Range": f"bytes */{total_size}"},
                status_code=416,
            )
        start, end = parsed_range
        try:
            result = s3.get_object(media["storage_key"], range_bytes=f"bytes={start}-{end}")
        except Exception as exc:
            raise HTTPException(status_code=404, detail="直播媒体对象不存在") from exc
        return asset_service.AssetStreamResult(
            body=result["Body"],
            media_type=str(result.get("ContentType") or stored_content_type),
            headers={
                **base_headers,
                "Content-Range": f"bytes {start}-{end}/{total_size}",
                "Content-Length": str(end - start + 1),
            },
            status_code=206,
        )

    try:
        result = s3.get_object(media["storage_key"])
    except Exception as exc:
        raise HTTPException(status_code=404, detail="直播媒体对象不存在") from exc
    headers = {**base_headers}
    length = result.get("ContentLength") or total_size
    if isinstance(length, int):
        headers["Content-Length"] = str(length)
    return asset_service.AssetStreamResult(
        body=result["Body"],
        media_type=str(result.get("ContentType") or stored_content_type),
        headers=headers,
    )


def get_live_dashboard(db: Session) -> dict[str, Any]:
    return {
        "settings": get_live_settings(db),
        "session": get_live_session(db),
        "playlist": get_live_playlist(db),
        "library_media": list_live_library_media(db),
        "audio_playlists": list_live_audio_playlists(db),
        "live_sources": list_live_input_sources(db),
        "completed_videos": list_completed_live_videos(db),
        "raw_videos": list_raw_live_videos(db),
    }


def _resolved_live_info(value: Any) -> dict[str, Any]:
    info = _as_dict(value)
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = info.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                data = _as_dict(entry)
                if data:
                    return data
        return {}
    return info


def _live_info_has_audio(info: dict[str, Any]) -> bool:
    if str(info.get("acodec") or "").lower() not in {"", "none"}:
        return True
    requested = info.get("requested_formats")
    return isinstance(requested, list) and any(
        str(_as_dict(item).get("acodec") or "").lower() not in {"", "none"} for item in requested
    )


def _resolve_live_input(settings: Any, page_url: str) -> ResolvedLiveInput:
    normalized_page_url = _normalize_live_input_url(page_url)
    resolve_public_endpoint(normalized_page_url)
    options = build_ydl_opts(settings, for_download=False)
    options.update(
        {
            "format": (
                "best[protocol^=m3u8][vcodec!=none][acodec!=none]/"
                "best[vcodec!=none][acodec!=none]/"
                "best[protocol^=m3u8][vcodec!=none]/best[vcodec!=none]"
            ),
            "skip_download": True,
            "noplaylist": True,
        }
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = _resolved_live_info(ydl.extract_info(normalized_page_url, download=False))
    except Exception as exc:
        raise RuntimeError(f"直播源解析失败: {type(exc).__name__}: {str(exc)[:300]}") from exc
    stream_url = _normalize_live_input_url(info.get("url"))
    resolve_public_endpoint(stream_url)

    raw_headers = _as_dict(info.get("http_headers"))
    allowed_headers = {"accept", "accept-language", "origin", "referer", "user-agent"}
    headers = {
        str(name): str(value).replace("\r", " ").replace("\n", " ").strip()
        for name, value in raw_headers.items()
        if str(name).strip().lower() in allowed_headers and str(value).strip()
    }
    return ResolvedLiveInput(url=stream_url, http_headers=headers, has_audio=_live_info_has_audio(info))


def _live_input_ffmpeg_args(settings: Any, resolved: ResolvedLiveInput) -> list[str]:
    args = [
        "-rw_timeout",
        "20000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
    ]
    proxy = str(getattr(settings, "youtube_proxy", None) or "").strip()
    if proxy and urlsplit(proxy).scheme.lower() in {"http", "https"}:
        args.extend(["-http_proxy", proxy])

    remaining_headers: list[str] = []
    for name, value in resolved.http_headers.items():
        if name.lower() == "user-agent":
            args.extend(["-user_agent", value])
        else:
            remaining_headers.append(f"{name}: {value}")
    if remaining_headers:
        args.extend(["-headers", "\r\n".join(remaining_headers) + "\r\n"])
    return args


def _validate_intel_live_encoder(ffmpeg_path: str, render_device: str) -> None:
    device = Path(str(render_device or "").strip() or "/dev/dri/renderD128")
    if not device.exists():
        raise FileNotFoundError(f"Intel GPU render device not found: {device}")
    try:
        result = subprocess.run(
            [str(ffmpeg_path or "ffmpeg"), "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"无法检查 FFmpeg Intel 编码器: {type(exc).__name__}") from exc
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or "h264_vaapi" not in output:
        raise RuntimeError("当前 FFmpeg 不支持 h264_vaapi，请安装 VAAPI 版本或关闭 Intel iGPU 硬件编码")


def _ffmpeg_command(
    *,
    ffmpeg_path: str,
    video_path: Path | str,
    audio_path: Path | None,
    config: dict[str, Any],
    target: str,
    mix_audio: bool = False,
    video_has_audio: bool = False,
    video_input_args: list[str] | None = None,
) -> list[str]:
    fps = int(config["fps"])
    video_bitrate = int(config["video_bitrate_kbps"])
    audio_bitrate = int(config["audio_bitrate_kbps"])
    keyframe = max(1, fps * int(config["keyframe_interval_seconds"]))
    use_intel_gpu = bool(config.get("use_intel_gpu", False))
    intel_gpu_render_device = str(config.get("intel_gpu_render_device") or "").strip() or "/dev/dri/renderD128"
    command = [
        str(ffmpeg_path or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
    ]
    if use_intel_gpu:
        command.extend(["-vaapi_device", intel_gpu_render_device])
    command.append("-re")
    command.extend(video_input_args or [])
    command.extend(["-i", str(video_path)])
    if audio_path is not None:
        command.extend(["-stream_loop", "-1", "-i", str(audio_path)])
        if mix_audio and video_has_audio:
            command.extend(
                [
                    "-filter_complex",
                    "[0:a:0][1:a:0]amix=inputs=2:duration=longest:dropout_transition=2:normalize=1[aout]",
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-shortest",
                ]
            )
        else:
            command.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest"])
    else:
        command.extend(["-map", "0:v:0", "-map", "0:a:0?"])
    if use_intel_gpu:
        command.extend(
            ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-rc_mode", "CBR", "-bf", "0"]
        )
    else:
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
            ]
        )
    command.extend(
        [
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


def _video_has_audio(ffmpeg_path: str, video_input: Path | str, video_input_args: list[str] | None = None) -> bool:
    ffmpeg_cmd = str(ffmpeg_path or "").strip() or "ffmpeg"
    ffmpeg_bin = Path(ffmpeg_cmd)
    ffprobe_name = "ffprobe" + ffmpeg_bin.suffix if ffmpeg_bin.suffix else "ffprobe"
    candidates = dict.fromkeys((str(ffmpeg_bin.with_name(ffprobe_name)), shutil.which("ffprobe") or "ffprobe"))
    for candidate in candidates:
        try:
            result = subprocess.run(
                [
                    candidate,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    *(video_input_args or []),
                    str(video_input),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            continue
        if result.returncode == 0:
            return bool(result.stdout.strip())
    logger.warning("unable to probe video audio stream: %s", video_input)
    return False


def _media_duration_seconds(
    ffmpeg_path: str,
    media_input: Path | str,
    media_input_args: list[str] | None = None,
) -> float | None:
    ffmpeg_cmd = str(ffmpeg_path or "").strip() or "ffmpeg"
    ffmpeg_bin = Path(ffmpeg_cmd)
    ffprobe_name = "ffprobe" + ffmpeg_bin.suffix if ffmpeg_bin.suffix else "ffprobe"
    candidates = dict.fromkeys((str(ffmpeg_bin.with_name(ffprobe_name)), shutil.which("ffprobe") or "ffprobe"))
    for candidate in candidates:
        try:
            result = subprocess.run(
                [
                    candidate,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    *(media_input_args or []),
                    str(media_input),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            duration = float(result.stdout.strip()) if result.returncode == 0 else 0.0
        except Exception:
            continue
        if duration > 0 and duration < 7 * 24 * 60 * 60:
            return duration
    return None


def _live_media_proxy_input(settings: Any, media: ResolvedMedia) -> tuple[str, list[str]]:
    """Return the controller's non-expiring, authenticated loopback media URL."""
    if media.is_live_source:
        raise ValueError("live input sources do not use the media proxy")
    base_url = str(getattr(settings, "live_internal_stream_base_url", "") or "").strip().rstrip("/")
    if not base_url:
        base_url = "http://127.0.0.1:8000"
    token = internal_header_token(settings)
    if not token:
        raise RuntimeError("直播媒体代理缺少内部服务凭据")
    url = f"{base_url}/live/media/{media.source.id}/stream"
    return url, ["-headers", f"{INTERNAL_TOKEN_HEADER}: {token}\r\n"]


def _remove_local_live_input(value: Path | str | None) -> None:
    """The S3 proxy has no local file, but retain safe cleanup for legacy paths."""
    if isinstance(value, Path):
        value.unlink(missing_ok=True)


def _preview_dir(work_dir: str | Path) -> Path:
    return Path(work_dir) / "live" / LIVE_PREVIEW_DIRECTORY


def live_preview_path(settings: Any, file_name: str) -> Path:
    """Return a safe path to the currently produced HLS preview artifact."""
    name = Path(str(file_name or "")).name
    if not name or name != file_name or name.startswith("."):
        raise HTTPException(status_code=404, detail="直播预览资源不存在")
    if name == LIVE_PREVIEW_PLAYLIST:
        return _preview_dir(settings.work_dir) / name
    if not name.startswith("segment_") or Path(name).suffix != ".ts":
        raise HTTPException(status_code=404, detail="直播预览资源不存在")
    return _preview_dir(settings.work_dir) / name


def _tee_escape(value: str) -> str:
    """Escape the two separators understood by FFmpeg's tee muxer."""
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _mixer_ffmpeg_command(
    *,
    ffmpeg_path: str,
    video_pipe: Path,
    source_audio_pipe: Path,
    music_audio_pipe: Path,
    config: dict[str, Any],
    target: str,
    preview_dir: Path,
) -> list[str]:
    """Build the long-lived output mixer.

    Producers write fixed-format raw media to the FIFOs.  This process owns
    the RTMP socket and the HLS preview, so replacing a producer never tears
    down the public stream connection.
    """
    fps = int(config["fps"])
    video_bitrate = int(config["video_bitrate_kbps"])
    audio_bitrate = int(config["audio_bitrate_kbps"])
    keyframe = max(1, fps * int(config["keyframe_interval_seconds"]))
    use_intel_gpu = bool(config.get("use_intel_gpu", False))
    intel_gpu_render_device = str(config.get("intel_gpu_render_device") or "").strip() or "/dev/dri/renderD128"
    segment_pattern = preview_dir / "segment_%06d.ts"
    hls_options = (
        "f=hls:hls_time=2:hls_list_size=6:"
        "hls_flags=delete_segments+append_list+independent_segments:"
        f"hls_segment_filename={_tee_escape(str(segment_pattern))}"
    )
    tee_target = f"[f=flv]{_tee_escape(target)}|[{hls_options}]{_tee_escape(str(preview_dir / LIVE_PREVIEW_PLAYLIST))}"
    command = [
        str(ffmpeg_path or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-thread_queue_size",
        "512",
        "-f",
        "rawvideo",
        "-pixel_format",
        "yuv420p",
        "-video_size",
        f"{LIVE_MIXER_WIDTH}x{LIVE_MIXER_HEIGHT}",
        "-framerate",
        str(fps),
        "-i",
        str(video_pipe),
        "-thread_queue_size",
        "512",
        "-f",
        "s16le",
        "-ar",
        str(LIVE_MIXER_AUDIO_RATE),
        "-ac",
        "2",
        "-i",
        str(source_audio_pipe),
        "-thread_queue_size",
        "512",
        "-f",
        "s16le",
        "-ar",
        str(LIVE_MIXER_AUDIO_RATE),
        "-ac",
        "2",
        "-i",
        str(music_audio_pipe),
        "-filter_complex",
        "[1:a:0][2:a:0]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[aout]",
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
    ]
    if use_intel_gpu:
        command[5:5] = ["-vaapi_device", intel_gpu_render_device]
        command.extend(["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-rc_mode", "CBR", "-bf", "0"])
    else:
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"])
    command.extend(
        [
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
            str(LIVE_MIXER_AUDIO_RATE),
            "-flvflags",
            "no_duration_filesize",
            "-f",
            "tee",
            tee_target,
        ]
    )
    return command


def _video_producer_command(
    *,
    ffmpeg_path: str,
    video_input: Path | str,
    video_input_args: list[str],
    fps: int,
    output_pipe: Path,
) -> list[str]:
    scale_filter = (
        f"scale={LIVE_MIXER_WIDTH}:{LIVE_MIXER_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={LIVE_MIXER_WIDTH}:{LIVE_MIXER_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},format=yuv420p"
    )
    return [
        str(ffmpeg_path or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-re",
        *video_input_args,
        "-i",
        str(video_input),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        scale_filter,
        "-c:v",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rawvideo",
        str(output_pipe),
    ]


def _source_audio_producer_command(
    *,
    ffmpeg_path: str,
    video_input: Path | str,
    video_input_args: list[str],
    output_pipe: Path,
    seek_seconds: float | None = None,
) -> list[str]:
    command = [str(ffmpeg_path or "ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin", "-y", "-re"]
    command.extend(video_input_args)
    if seek_seconds and seek_seconds > 0:
        command.extend(["-ss", f"{seek_seconds:.3f}"])
    command.extend(
        [
            "-i",
            str(video_input),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(LIVE_MIXER_AUDIO_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            str(output_pipe),
        ]
    )
    return command


def _music_audio_producer_command(
    *,
    ffmpeg_path: str,
    audio_input: Path | str,
    audio_input_args: list[str] | None = None,
    output_pipe: Path,
    seek_seconds: float | None = None,
    volume_percent: int = 100,
    loop: bool = False,
) -> list[str]:
    command = [
        str(ffmpeg_path or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-re",
        *(audio_input_args or []),
    ]
    # A music producer must reach EOF so the controller can select the next
    # item from the real queue.  Looping here made every queue look like it
    # contained one endlessly repeating song.
    if loop:
        command[command.index("-re") + 1 : command.index("-re") + 1] = ["-stream_loop", "-1"]
    if seek_seconds and seek_seconds > 0:
        command.extend(["-ss", f"{seek_seconds:.3f}"])
    volume = max(0, min(200, int(volume_percent))) / 100
    command.extend(
        [
            "-i",
            str(audio_input),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(LIVE_MIXER_AUDIO_RATE),
            "-af",
            f"volume={volume:.2f}",
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            str(output_pipe),
        ]
    )
    return command


def _silence_audio_producer_command(*, ffmpeg_path: str, output_pipe: Path) -> list[str]:
    return [
        str(ffmpeg_path or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout=stereo:sample_rate={LIVE_MIXER_AUDIO_RATE}",
        "-ac",
        "2",
        "-ar",
        str(LIVE_MIXER_AUDIO_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        str(output_pipe),
    ]


class LiveStreamController:
    """One continuously-connected output mixer with replaceable media producers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._mixer_process: subprocess.Popen[Any] | None = None
        self._video_process: subprocess.Popen[Any] | None = None
        self._source_audio_process: subprocess.Popen[Any] | None = None
        self._music_audio_process: subprocess.Popen[Any] | None = None
        self._fifo_keepalives: list[int] = []
        self._paused = False
        self._session_id: str | None = None
        self._pending_playlist: dict[str, Any] | None = None
        self._switch_requested = threading.Event()
        self._pending_audio_control: dict[str, Any] | None = None
        self._audio_control_requested = threading.Event()
        self._audio_playback: dict[str, Any] = {
            "audio_player_status": "idle",
            "audio_position_seconds": 0.0,
            "audio_duration_seconds": None,
            "_position_base_seconds": 0.0,
            "_started_monotonic": None,
        }

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
            self._pending_playlist = None
            self._switch_requested.clear()
            self._pending_audio_control = None
            self._audio_control_requested.clear()
            self._audio_playback = {
                "audio_player_status": "idle",
                "audio_position_seconds": 0.0,
                "audio_duration_seconds": None,
                "_position_base_seconds": 0.0,
                "_started_monotonic": None,
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(settings, session_id, config, target, playlist),
                name="videoroll-live-stream",
                daemon=True,
            )
            self._thread.start()

    def switch(self, playlist: dict[str, Any]) -> None:
        """Replace a producer input while the output mixer keeps the RTMP socket."""
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                raise HTTPException(status_code=409, detail="当前没有运行中的直播")
            self._pending_playlist = playlist
            self._paused = False
            self._switch_requested.set()
            self._wake.set()

    def control_audio(self, control: dict[str, Any]) -> None:
        """Queue an independent-audio operation without touching the mixer or video."""
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                raise HTTPException(status_code=409, detail="当前没有运行中的直播")
            self._pending_audio_control = control
            self._audio_control_requested.set()
            self._wake.set()

    def _take_pending_audio_control(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._audio_control_requested.is_set():
                return None
            self._audio_control_requested.clear()
            return self._pending_audio_control

    @staticmethod
    def _normalized_audio_position(value: float, duration: float | None) -> float:
        if not duration or duration <= 0:
            return max(0.0, value)
        return max(0.0, value) % duration

    def _set_audio_playback(
        self,
        *,
        status: str,
        position_seconds: float,
        duration_seconds: float | None,
        started_monotonic: float | None,
    ) -> None:
        with self._lock:
            self._audio_playback = {
                "audio_player_status": status if status in {"idle", "playing", "paused"} else "idle",
                "audio_position_seconds": max(0.0, float(position_seconds or 0.0)),
                "audio_duration_seconds": duration_seconds if duration_seconds and duration_seconds > 0 else None,
                "_position_base_seconds": max(0.0, float(position_seconds or 0.0)),
                "_started_monotonic": started_monotonic,
            }

    def audio_playback_state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._audio_playback)
        status = str(state.get("audio_player_status") or "idle")
        position = float(state.get("_position_base_seconds") or 0.0)
        started = state.get("_started_monotonic")
        duration = state.get("audio_duration_seconds")
        if status == "playing" and isinstance(started, (int, float)):
            position += max(0.0, time.monotonic() - float(started))
        position = self._normalized_audio_position(position, duration if isinstance(duration, (int, float)) else None)
        return {
            "audio_player_status": status if status in {"idle", "playing", "paused"} else "idle",
            "audio_position_seconds": position,
            "audio_duration_seconds": duration if isinstance(duration, (int, float)) and duration > 0 else None,
        }

    def pause(self) -> None:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                raise HTTPException(status_code=409, detail="当前没有运行中的直播")
            self._paused = True
            self._wake.set()
        self._terminate_runtime(include_mixer=True)

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
        self._terminate_runtime(include_mixer=True)

    def _paused_now(self) -> bool:
        with self._lock:
            return self._paused

    def _take_pending_playlist(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._switch_requested.is_set():
                return None
            self._switch_requested.clear()
            return self._pending_playlist

    def _set_process(self, name: str, process: subprocess.Popen[Any] | None) -> None:
        with self._lock:
            setattr(self, name, process)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[Any] | None) -> None:
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning("live FFmpeg process did not exit after SIGKILL")

    def _terminate_runtime(self, *, include_mixer: bool) -> None:
        with self._lock:
            processes = [self._video_process, self._source_audio_process, self._music_audio_process]
            self._video_process = None
            self._source_audio_process = None
            self._music_audio_process = None
            if include_mixer:
                processes.append(self._mixer_process)
                self._mixer_process = None
        for process in processes:
            self._terminate_process(process)

    def _terminate_video_inputs(self) -> None:
        """Replace only the video and its original-audio input.

        The independent music FIFO is intentionally left running so a song
        can continue over a video boundary just like it would in a normal
        player.  Full stop/pause and explicit source changes still use
        ``_terminate_runtime``.
        """
        with self._lock:
            processes = [self._video_process, self._source_audio_process]
            self._video_process = None
            self._source_audio_process = None
        for process in processes:
            self._terminate_process(process)

    def _close_fifos(self) -> None:
        with self._lock:
            fds = self._fifo_keepalives
            self._fifo_keepalives = []
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

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

    @classmethod
    def _order_from_selected_audio(
        cls,
        sources: list[LiveSource],
        selected: LiveSource,
        mode: str,
    ) -> tuple[list[int], int]:
        """Build a complete queue with ``selected`` as the current song.

        The selected track is a cursor inside the queue, never a replacement
        for it.  This mirrors normal music-player behaviour: clicking a song
        starts that song, then continues through the remaining playlist.
        """
        try:
            selected_index = sources.index(selected)
        except ValueError as exc:  # pragma: no cover - request validation prevents this
            raise RuntimeError("所选歌曲不在当前播放队列中") from exc
        if mode == "shuffle":
            remaining = [index for index in range(len(sources)) if index != selected_index]
            random.shuffle(remaining)
            return [selected_index, *remaining], 0
        return list(range(len(sources))), selected_index

    @classmethod
    def _advance_audio_queue(
        cls,
        sources: list[LiveSource],
        order: list[int],
        position: int,
        mode: str,
    ) -> tuple[list[int], int]:
        """Return the next queue cursor, reshuffling only after one full pass."""
        if not sources:
            return [], 0
        if position + 1 < len(order):
            return order, position + 1
        return cls._ordered_indices(len(sources), mode), 0

    @staticmethod
    def _spawn(command: list[str], log_path: Path) -> subprocess.Popen[Any]:
        with log_path.open("ab") as log_file:
            return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT)

    def _start_mixer(self, settings: Any, *, work_dir: Path, config: dict[str, Any], target: str) -> dict[str, Path]:
        pipes_dir = work_dir / "pipes"
        pipes_dir.mkdir(parents=True, exist_ok=True)
        pipes = {
            "video": pipes_dir / "video.yuv",
            "source_audio": pipes_dir / "source_audio.pcm",
            "music_audio": pipes_dir / "music_audio.pcm",
        }
        for pipe in pipes.values():
            pipe.unlink(missing_ok=True)
            os.mkfifo(pipe, 0o600)
        keepalives = [os.open(pipe, os.O_RDWR | os.O_NONBLOCK) for pipe in pipes.values()]
        with self._lock:
            self._fifo_keepalives = keepalives
        preview_dir = _preview_dir(settings.work_dir)
        shutil.rmtree(preview_dir, ignore_errors=True)
        preview_dir.mkdir(parents=True, exist_ok=True)
        command = _mixer_ffmpeg_command(
            ffmpeg_path=settings.ffmpeg_path,
            video_pipe=pipes["video"],
            source_audio_pipe=pipes["source_audio"],
            music_audio_pipe=pipes["music_audio"],
            config=config,
            target=target,
            preview_dir=preview_dir,
        )
        process = self._spawn(command, work_dir / "mixer.log")
        self._set_process("_mixer_process", process)
        return pipes

    def _start_video_producer(
        self,
        settings: Any,
        *,
        work_dir: Path,
        video_input: Path | str,
        video_input_args: list[str],
        fps: int,
        output_pipe: Path,
    ) -> None:
        process = self._spawn(
            _video_producer_command(
                ffmpeg_path=settings.ffmpeg_path,
                video_input=video_input,
                video_input_args=video_input_args,
                fps=fps,
                output_pipe=output_pipe,
            ),
            work_dir / "video-producer.log",
        )
        self._set_process("_video_process", process)

    def _start_source_audio_producer(
        self,
        settings: Any,
        *,
        work_dir: Path,
        video_input: Path | str,
        video_input_args: list[str],
        output_pipe: Path,
        include_source_audio: bool,
        source_has_audio: bool,
        seek_seconds: float | None = None,
    ) -> None:
        if include_source_audio and source_has_audio:
            command = _source_audio_producer_command(
                ffmpeg_path=settings.ffmpeg_path,
                video_input=video_input,
                video_input_args=video_input_args,
                output_pipe=output_pipe,
                seek_seconds=seek_seconds,
            )
        else:
            command = _silence_audio_producer_command(ffmpeg_path=settings.ffmpeg_path, output_pipe=output_pipe)
        self._set_process("_source_audio_process", self._spawn(command, work_dir / "source-audio-producer.log"))

    def _start_music_audio_producer(
        self,
        settings: Any,
        *,
        work_dir: Path,
        audio_input: Path | str | None,
        audio_input_args: list[str],
        output_pipe: Path,
        seek_seconds: float | None = None,
        volume_percent: int = 100,
    ) -> None:
        command = (
            _music_audio_producer_command(
                ffmpeg_path=settings.ffmpeg_path,
                audio_input=audio_input,
                audio_input_args=audio_input_args,
                output_pipe=output_pipe,
                seek_seconds=seek_seconds,
                volume_percent=volume_percent,
            )
            if audio_input is not None
            else _silence_audio_producer_command(ffmpeg_path=settings.ffmpeg_path, output_pipe=output_pipe)
        )
        self._set_process("_music_audio_process", self._spawn(command, work_dir / "music-audio-producer.log"))

    @staticmethod
    def _source_audio_enabled(*, audio: ResolvedMedia | None, mix_audio: bool) -> bool:
        return audio is None or mix_audio

    def _resolve_media_pair(
        self,
        settings: Any,
        *,
        work_dir: Path,
        video_source: LiveSource,
        audio_source: LiveSource | None,
        sequence: int,
    ) -> tuple[
        ResolvedMedia,
        ResolvedMedia | None,
        Path | None,
        Path | str,
        list[str],
        bool,
        Path | str | None,
        list[str],
    ]:
        db = get_sessionmaker(settings.database_url)()
        try:
            video = _resolve_media(db, video_source, expected_type="video")
            audio = _resolve_media(db, audio_source, expected_type="audio") if audio_source else None
        finally:
            db.close()

        video_path: Path | None = None
        video_input: Path | str
        video_input_args: list[str] = []
        source_has_audio = False
        if video.is_live_source:
            resolved_input = _resolve_live_input(settings, video.storage_key)
            video_input = resolved_input.url
            video_input_args = _live_input_ffmpeg_args(settings, resolved_input)
            source_has_audio = resolved_input.has_audio
        else:
            video_input, video_input_args = _live_media_proxy_input(settings, video)
            source_has_audio = _video_has_audio(settings.ffmpeg_path, video_input, video_input_args)
        audio_input: Path | str | None = None
        audio_input_args: list[str] = []
        if audio:
            audio_input, audio_input_args = _live_media_proxy_input(settings, audio)
        return video, audio, video_path, video_input, video_input_args, source_has_audio, audio_input, audio_input_args

    @staticmethod
    def _resolve_library_audio_input(
        settings: Any,
        audio_source: LiveSource,
    ) -> tuple[ResolvedMedia, Path | str, list[str], float | None]:
        db = get_sessionmaker(settings.database_url)()
        try:
            audio = _resolve_media(db, audio_source, expected_type="audio")
        finally:
            db.close()
        audio_input, audio_input_args = _live_media_proxy_input(settings, audio)
        duration = _media_duration_seconds(settings.ffmpeg_path, audio_input, audio_input_args)
        return audio, audio_input, audio_input_args, duration

    def _run(self, settings: Any, session_id: str, config: dict[str, Any], target: str, playlist: dict[str, Any]) -> None:
        work_dir = Path(settings.work_dir) / "live" / session_id
        pipes: dict[str, Path] | None = None
        video_sources = [_normalize_source(item) for item in playlist["video_items"]]
        audio_sources = [_normalize_source(item) for item in playlist["audio_items"]]
        mode = str(playlist.get("playback_mode") or "sequential")
        audio_playback_mode = str(playlist.get("audio_playback_mode") or "sequential")
        if audio_playback_mode not in {"sequential", "shuffle"}:
            audio_playback_mode = "sequential"
        loop_playlist = bool(playlist.get("loop_playlist", True))
        mix_audio = bool(playlist.get("mix_audio", False))
        video_order = self._ordered_indices(len(video_sources), mode)
        audio_order = self._ordered_indices(len(audio_sources), audio_playback_mode) if audio_sources else []
        video_position = 0
        audio_position = 0
        music_volume_percent = 100
        shuffle_history: list[LiveSource] = []
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            self._persist(settings, status="running", started_at=_now_iso(), stopped_at=None, last_error=None)
            while not self._stop_requested.is_set():
                if not self._wait_while_paused(settings):
                    break
                if pipes is None:
                    pipes = self._start_mixer(settings, work_dir=work_dir, config=config, target=target)
                if video_position >= len(video_order):
                    if not loop_playlist:
                        break
                    video_order = self._ordered_indices(len(video_sources), mode)
                    video_position = 0
                if audio_sources and audio_position >= len(audio_order):
                    audio_order = self._ordered_indices(len(audio_sources), audio_playback_mode)
                    audio_position = 0
                if not video_order:
                    raise RuntimeError("直播播放列表中没有视频资源")

                video_source = video_sources[video_order[video_position]]
                audio_source = audio_sources[audio_order[audio_position]] if audio_sources else None
                (
                    video,
                    audio,
                    video_path,
                    video_input,
                    video_input_args,
                    source_has_audio,
                    audio_input,
                    audio_input_args,
                ) = self._resolve_media_pair(
                    settings,
                    work_dir=work_dir,
                    video_source=video_source,
                    audio_source=audio_source,
                    sequence=video_position,
                )
                include_source_audio = self._source_audio_enabled(audio=audio, mix_audio=mix_audio)
                music_position_seconds = 0.0
                music_duration_seconds = (
                    _media_duration_seconds(settings.ffmpeg_path, audio_input, audio_input_args) if audio_input is not None else None
                )
                existing_music_state = self.audio_playback_state()
                with self._lock:
                    existing_music_process = self._music_audio_process
                reuse_music = bool(
                    audio is not None
                    and existing_music_process is not None
                    and existing_music_process.poll() is None
                    and existing_music_state["audio_player_status"] in {"playing", "paused"}
                )
                music_paused = reuse_music and existing_music_state["audio_player_status"] == "paused"
                if reuse_music:
                    music_position_seconds = float(existing_music_state["audio_position_seconds"])
                    previous_duration = existing_music_state.get("audio_duration_seconds")
                    if isinstance(previous_duration, (int, float)) and previous_duration > 0:
                        music_duration_seconds = float(previous_duration)
                music_started_monotonic = None if music_paused else time.monotonic()
                self._start_video_producer(
                    settings,
                    work_dir=work_dir,
                    video_input=video_input,
                    video_input_args=video_input_args,
                    fps=int(config["fps"]),
                    output_pipe=pipes["video"],
                )
                self._start_source_audio_producer(
                    settings,
                    work_dir=work_dir,
                    video_input=video_input,
                    video_input_args=video_input_args,
                    output_pipe=pipes["source_audio"],
                    include_source_audio=include_source_audio,
                    source_has_audio=source_has_audio,
                )
                if not reuse_music:
                    self._start_music_audio_producer(
                        settings,
                        work_dir=work_dir,
                        audio_input=audio_input,
                        audio_input_args=audio_input_args,
                        output_pipe=pipes["music_audio"],
                        volume_percent=music_volume_percent,
                    )
                started_monotonic = time.monotonic()
                self._persist(
                    settings,
                    status="running",
                    current_video={"source": video.source.source, "id": video.source.id, "display_name": video.display_name},
                current_audio=(
                    {"source": audio.source.source, "id": audio.source.id, "display_name": audio.display_name} if audio else None
                ),
                mix_audio=mix_audio,
                audio_player_status="playing" if audio else "idle",
                audio_position_seconds=music_position_seconds,
                audio_duration_seconds=music_duration_seconds,
                audio_playback_mode=audio_playback_mode,
                audio_volume_percent=music_volume_percent,
                )
                self._set_audio_playback(
                    status="playing" if audio else "idle",
                    position_seconds=music_position_seconds,
                    duration_seconds=music_duration_seconds,
                    started_monotonic=music_started_monotonic if audio else None,
                )

                start_next_video = False
                restart_mixer = False
                while not self._stop_requested.is_set():
                    if self._paused_now():
                        self._terminate_runtime(include_mixer=True)
                        restart_mixer = True
                        break
                    audio_control = self._take_pending_audio_control()
                    if audio_control is not None:
                        action = str(audio_control.get("action") or "").strip().lower()
                        queue_sources = [_normalize_source(item) for item in (audio_control.get("audio_items") or [])]
                        if action == "set_playback_mode":
                            requested_mode = str(audio_control.get("playback_mode") or "").strip().lower()
                            if requested_mode not in {"sequential", "shuffle"}:
                                raise RuntimeError("未知的音频播放模式")
                            audio_playback_mode = requested_mode
                            shuffle_history = []
                            if audio_source is not None and audio_sources:
                                audio_order, audio_position = self._order_from_selected_audio(
                                    audio_sources,
                                    audio_source,
                                    audio_playback_mode,
                                )
                            self._persist(settings, audio_playback_mode=audio_playback_mode)
                            continue
                        if action == "set_volume":
                            music_volume_percent = _bounded_int(
                                audio_control.get("volume_percent"),
                                default=music_volume_percent,
                                minimum=0,
                                maximum=200,
                                field="独立音频音量",
                            )
                            if audio is not None and audio_input is not None and not music_paused:
                                current_position = self.audio_playback_state()["audio_position_seconds"]
                                with self._lock:
                                    old_music_process = self._music_audio_process
                                    self._music_audio_process = None
                                self._terminate_process(old_music_process)
                                music_position_seconds = float(current_position)
                                music_started_monotonic = time.monotonic()
                                self._start_music_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    audio_input=audio_input,
                                    audio_input_args=audio_input_args,
                                    output_pipe=pipes["music_audio"],
                                    seek_seconds=music_position_seconds,
                                    volume_percent=music_volume_percent,
                                )
                            self._persist(settings, audio_volume_percent=music_volume_percent)
                            continue
                        if action == "original":
                            if not include_source_audio:
                                with self._lock:
                                    old_source_audio_process = self._source_audio_process
                                    self._source_audio_process = None
                                self._terminate_process(old_source_audio_process)
                                self._start_source_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    video_input=video_input,
                                    video_input_args=video_input_args,
                                    output_pipe=pipes["source_audio"],
                                    include_source_audio=True,
                                    source_has_audio=source_has_audio,
                                    seek_seconds=time.monotonic() - started_monotonic,
                                )
                                include_source_audio = True
                            with self._lock:
                                old_music_process = self._music_audio_process
                                self._music_audio_process = None
                            self._terminate_process(old_music_process)
                            self._start_music_audio_producer(
                                settings,
                                work_dir=work_dir,
                                audio_input=None,
                                audio_input_args=[],
                                output_pipe=pipes["music_audio"],
                            )
                            audio = None
                            audio_source = None
                            audio_input = None
                            audio_input_args = []
                            # Keep subsequent automatic video changes on the
                            # original soundtrack too; a later explicit song
                            # choice can still re-enable independent audio.
                            audio_sources = []
                            audio_order = []
                            audio_position = 0
                            mix_audio = False
                            music_position_seconds = 0.0
                            music_duration_seconds = None
                            music_paused = False
                            music_started_monotonic = None
                            self._set_audio_playback(
                                status="idle",
                                position_seconds=0.0,
                                duration_seconds=None,
                                started_monotonic=None,
                            )
                            self._persist(
                                settings,
                                current_audio=None,
                                mix_audio=False,
                                audio_player_status="idle",
                                audio_position_seconds=0.0,
                                audio_duration_seconds=None,
                            )
                            continue
                        if action in {"play", "next", "previous"}:
                            if action == "play":
                                selected = audio_control.get("audio_item")
                                if selected is None:
                                    raise RuntimeError("播放歌曲时缺少音频资源")
                                target_audio_source = _normalize_source(selected)
                                # The browser sends the visible player queue
                                # together with a selected song.  Keep that
                                # queue so a manual selection continues to
                                # the following track instead of becoming a
                                # one-item endless loop.
                                if queue_sources:
                                    if target_audio_source not in queue_sources:
                                        raise RuntimeError("所选歌曲不在当前播放队列中")
                                    audio_sources = queue_sources
                                else:
                                    audio_sources = [target_audio_source]
                                music_paused = False
                            else:
                                if not queue_sources:
                                    raise RuntimeError("切换歌曲时缺少歌单队列")
                                requested_mode = str(audio_control.get("playback_mode") or audio_playback_mode).strip().lower()
                                if requested_mode in {"sequential", "shuffle"}:
                                    audio_playback_mode = requested_mode
                                if audio_playback_mode == "shuffle":
                                    if action == "previous" and shuffle_history:
                                        target_audio_source = shuffle_history.pop()
                                    else:
                                        if action == "next" and audio_source is not None:
                                            shuffle_history.append(audio_source)
                                        choices = [item for item in queue_sources if item != audio_source] or queue_sources
                                        target_audio_source = random.choice(choices)
                                else:
                                    shuffle_history = []
                                    try:
                                        current_index = queue_sources.index(audio_source) if audio_source is not None else -1
                                    except ValueError:
                                        current_index = -1
                                    direction = 1 if action == "next" else -1
                                    target_audio_source = queue_sources[(current_index + direction) % len(queue_sources)]
                                audio_sources = queue_sources
                            if target_audio_source.source != "library":
                                raise RuntimeError("直播歌曲必须来自音频媒体库")
                            replacement_audio, replacement_audio_input, replacement_audio_input_args, replacement_duration = self._resolve_library_audio_input(
                                settings,
                                target_audio_source,
                            )
                            replacement_include_source = self._source_audio_enabled(audio=replacement_audio, mix_audio=mix_audio)
                            if replacement_include_source != include_source_audio:
                                with self._lock:
                                    old_source_audio_process = self._source_audio_process
                                    self._source_audio_process = None
                                self._terminate_process(old_source_audio_process)
                                self._start_source_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    video_input=video_input,
                                    video_input_args=video_input_args,
                                    output_pipe=pipes["source_audio"],
                                    include_source_audio=replacement_include_source,
                                    source_has_audio=source_has_audio,
                                    seek_seconds=time.monotonic() - started_monotonic,
                                )
                                include_source_audio = replacement_include_source
                            with self._lock:
                                old_music_process = self._music_audio_process
                                self._music_audio_process = None
                            self._terminate_process(old_music_process)
                            audio = replacement_audio
                            audio_source = target_audio_source
                            audio_input = replacement_audio_input
                            audio_input_args = replacement_audio_input_args
                            audio_order, audio_position = self._order_from_selected_audio(
                                audio_sources,
                                target_audio_source,
                                audio_playback_mode,
                            )
                            music_position_seconds = 0.0
                            music_duration_seconds = replacement_duration
                            if music_paused:
                                self._start_music_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    audio_input=None,
                                    audio_input_args=[],
                                    output_pipe=pipes["music_audio"],
                                )
                                music_started_monotonic = None
                            else:
                                self._start_music_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    audio_input=audio_input,
                                    audio_input_args=audio_input_args,
                                    output_pipe=pipes["music_audio"],
                                    volume_percent=music_volume_percent,
                                )
                                music_started_monotonic = time.monotonic()
                            self._set_audio_playback(
                                status="paused" if music_paused else "playing",
                                position_seconds=music_position_seconds,
                                duration_seconds=music_duration_seconds,
                                started_monotonic=music_started_monotonic,
                            )
                            self._persist(
                                settings,
                                current_audio={"source": audio.source.source, "id": audio.source.id, "display_name": audio.display_name},
                                audio_player_status="paused" if music_paused else "playing",
                                audio_position_seconds=music_position_seconds,
                                audio_duration_seconds=music_duration_seconds,
                                audio_playback_mode=audio_playback_mode,
                                audio_volume_percent=music_volume_percent,
                            )
                            continue
                        if audio is None or audio_input is None:
                            raise RuntimeError("当前没有可控制的独立音频")
                        current_position = self.audio_playback_state()["audio_position_seconds"]
                        if action == "pause":
                            if not music_paused:
                                with self._lock:
                                    old_music_process = self._music_audio_process
                                    self._music_audio_process = None
                                self._terminate_process(old_music_process)
                                music_position_seconds = float(current_position)
                                music_paused = True
                                music_started_monotonic = None
                                self._start_music_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    audio_input=None,
                                    audio_input_args=[],
                                    output_pipe=pipes["music_audio"],
                                )
                        elif action == "resume":
                            if music_paused:
                                with self._lock:
                                    old_music_process = self._music_audio_process
                                    self._music_audio_process = None
                                self._terminate_process(old_music_process)
                                music_paused = False
                                music_started_monotonic = time.monotonic()
                                self._start_music_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    audio_input=audio_input,
                                    audio_input_args=audio_input_args,
                                    output_pipe=pipes["music_audio"],
                                    seek_seconds=music_position_seconds,
                                    volume_percent=music_volume_percent,
                                )
                        elif action == "seek":
                            requested_position = float(audio_control.get("position_seconds") or 0.0)
                            music_position_seconds = self._normalized_audio_position(requested_position, music_duration_seconds)
                            if not music_paused:
                                with self._lock:
                                    old_music_process = self._music_audio_process
                                    self._music_audio_process = None
                                self._terminate_process(old_music_process)
                                music_started_monotonic = time.monotonic()
                                self._start_music_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    audio_input=audio_input,
                                    audio_input_args=audio_input_args,
                                    output_pipe=pipes["music_audio"],
                                    seek_seconds=music_position_seconds,
                                    volume_percent=music_volume_percent,
                                )
                        else:
                            raise RuntimeError("未知的音频播放控制操作")
                        self._set_audio_playback(
                            status="paused" if music_paused else "playing",
                            position_seconds=music_position_seconds,
                            duration_seconds=music_duration_seconds,
                            started_monotonic=music_started_monotonic,
                        )
                        self._persist(
                            settings,
                            audio_player_status="paused" if music_paused else "playing",
                            audio_position_seconds=music_position_seconds,
                            audio_duration_seconds=music_duration_seconds,
                        )
                        continue
                    replacement_playlist = self._take_pending_playlist()
                    if replacement_playlist is not None:
                        replacement_videos = [_normalize_source(item) for item in replacement_playlist["video_items"]]
                        replacement_audios = [_normalize_source(item) for item in replacement_playlist["audio_items"]]
                        if not replacement_videos:
                            raise RuntimeError("切换直播资源时缺少视频")
                        replacement_video_source = replacement_videos[0]
                        replacement_audio_source = replacement_audios[0] if replacement_audios else None
                        replacement_mix_audio = bool(replacement_playlist.get("mix_audio", False))
                        replacement_audio_mode = str(replacement_playlist.get("audio_playback_mode") or "sequential")
                        if replacement_audio_mode not in {"sequential", "shuffle"}:
                            replacement_audio_mode = "sequential"
                        if replacement_video_source == video_source:
                            db = get_sessionmaker(settings.database_url)()
                            try:
                                replacement_audio = (
                                    _resolve_media(db, replacement_audio_source, expected_type="audio") if replacement_audio_source else None
                                )
                            finally:
                                db.close()
                            replacement_audio_input: Path | str | None = None
                            replacement_audio_input_args: list[str] = []
                            if replacement_audio:
                                replacement_audio_input, replacement_audio_input_args = _live_media_proxy_input(
                                    settings,
                                    replacement_audio,
                                )
                            replacement_include_source = self._source_audio_enabled(
                                audio=replacement_audio,
                                mix_audio=replacement_mix_audio,
                            )
                            if replacement_include_source != include_source_audio:
                                with self._lock:
                                    old_source_audio_process = self._source_audio_process
                                    self._source_audio_process = None
                                self._terminate_process(old_source_audio_process)
                                self._start_source_audio_producer(
                                    settings,
                                    work_dir=work_dir,
                                    video_input=video_input,
                                    video_input_args=video_input_args,
                                    output_pipe=pipes["source_audio"],
                                    include_source_audio=replacement_include_source,
                                    source_has_audio=source_has_audio,
                                    seek_seconds=time.monotonic() - started_monotonic,
                                )
                                include_source_audio = replacement_include_source
                            with self._lock:
                                old_music_process = self._music_audio_process
                                self._music_audio_process = None
                            self._terminate_process(old_music_process)
                            _remove_local_live_input(audio_input)
                            audio_input = replacement_audio_input
                            audio_input_args = replacement_audio_input_args
                            audio = replacement_audio
                            audio_source = replacement_audio_source
                            music_position_seconds = 0.0
                            music_duration_seconds = (
                                _media_duration_seconds(settings.ffmpeg_path, audio_input, audio_input_args)
                                if audio_input is not None
                                else None
                            )
                            music_paused = False
                            music_started_monotonic = time.monotonic()
                            self._start_music_audio_producer(
                                settings,
                                work_dir=work_dir,
                                audio_input=audio_input,
                                audio_input_args=audio_input_args,
                                output_pipe=pipes["music_audio"],
                                volume_percent=music_volume_percent,
                            )
                            playlist = replacement_playlist
                            video_sources = replacement_videos
                            audio_sources = replacement_audios
                            mode = str(playlist.get("playback_mode") or "sequential")
                            loop_playlist = bool(playlist.get("loop_playlist", True))
                            mix_audio = replacement_mix_audio
                            audio_playback_mode = replacement_audio_mode
                            shuffle_history = []
                            video_order = [0]
                            audio_order = self._ordered_indices(len(audio_sources), audio_playback_mode) if audio_sources else []
                            video_position = 0
                            audio_position = 0
                            self._persist(
                                settings,
                                status="running",
                                current_audio=(
                                    {"source": audio.source.source, "id": audio.source.id, "display_name": audio.display_name} if audio else None
                                ),
                                mix_audio=mix_audio,
                                audio_playback_mode=audio_playback_mode,
                                audio_volume_percent=music_volume_percent,
                                audio_player_status="playing" if audio else "idle",
                                audio_position_seconds=music_position_seconds,
                                audio_duration_seconds=music_duration_seconds,
                            )
                            self._set_audio_playback(
                                status="playing" if audio else "idle",
                                position_seconds=music_position_seconds,
                                duration_seconds=music_duration_seconds,
                                started_monotonic=music_started_monotonic if audio else None,
                            )
                            continue
                        self._terminate_runtime(include_mixer=False)
                        for media_input in (video_path, audio_input):
                            _remove_local_live_input(media_input)
                        playlist = replacement_playlist
                        video_sources = replacement_videos
                        audio_sources = replacement_audios
                        mode = str(playlist.get("playback_mode") or "sequential")
                        loop_playlist = bool(playlist.get("loop_playlist", True))
                        mix_audio = replacement_mix_audio
                        audio_playback_mode = replacement_audio_mode
                        shuffle_history = []
                        video_order = [0]
                        audio_order = self._ordered_indices(len(audio_sources), audio_playback_mode) if audio_sources else []
                        video_position = 0
                        audio_position = 0
                        start_next_video = True
                        break
                    with self._lock:
                        mixer_process = self._mixer_process
                        video_process = self._video_process
                        music_process = self._music_audio_process
                    if mixer_process is None or mixer_process.poll() is not None:
                        raise RuntimeError("FFmpeg 混流器异常退出；RTMP 推流已停止")
                    if video_process is None:
                        raise RuntimeError("视频输入进程未启动")
                    # Unlike the silent/source-audio producers, a music
                    # producer intentionally reaches EOF at the end of a
                    # song.  Advance it independently of the video producer
                    # so the music queue behaves like a normal player.
                    if music_process is not None and music_process.poll() is not None and audio is not None and not music_paused:
                        music_return_code = music_process.returncode
                        if music_return_code not in {0, None}:
                            logger.warning("live music input exited early (exit=%s); skipping to the next track", music_return_code)
                        if audio_sources:
                            audio_order, audio_position = self._advance_audio_queue(
                                audio_sources,
                                audio_order,
                                audio_position,
                                audio_playback_mode,
                            )
                            next_audio_source = audio_sources[audio_order[audio_position]]
                            (
                                next_audio,
                                next_audio_input,
                                next_audio_input_args,
                                next_duration,
                            ) = self._resolve_library_audio_input(settings, next_audio_source)
                            audio = next_audio
                            audio_source = next_audio_source
                            audio_input = next_audio_input
                            audio_input_args = next_audio_input_args
                            music_position_seconds = 0.0
                            music_duration_seconds = next_duration
                            music_started_monotonic = time.monotonic()
                            self._start_music_audio_producer(
                                settings,
                                work_dir=work_dir,
                                audio_input=audio_input,
                                audio_input_args=audio_input_args,
                                output_pipe=pipes["music_audio"],
                                volume_percent=music_volume_percent,
                            )
                            self._set_audio_playback(
                                status="playing",
                                position_seconds=music_position_seconds,
                                duration_seconds=music_duration_seconds,
                                started_monotonic=music_started_monotonic,
                            )
                            self._persist(
                                settings,
                                current_audio={
                                    "source": audio.source.source,
                                    "id": audio.source.id,
                                    "display_name": audio.display_name,
                                },
                                audio_player_status="playing",
                                audio_position_seconds=music_position_seconds,
                                audio_duration_seconds=music_duration_seconds,
                                audio_playback_mode=audio_playback_mode,
                                audio_volume_percent=music_volume_percent,
                            )
                            continue
                    return_code = video_process.poll()
                    if return_code is not None:
                        if return_code != 0:
                            raise RuntimeError(f"FFmpeg 视频输入异常退出（exit={return_code}）")
                        if audio is None:
                            # There is only the long-running silence producer
                            # in this case, so replace all non-mixer inputs.
                            self._terminate_runtime(include_mixer=False)
                        else:
                            # Keep an independent song playing across video
                            # changes; only the video and its original audio
                            # need to be replaced.
                            self._terminate_video_inputs()
                        _remove_local_live_input(video_path)
                        video_position += 1
                        start_next_video = True
                        break
                    time.sleep(0.1)

                if self._stop_requested.is_set():
                    break
                if restart_mixer:
                    for media_input in (video_path, audio_input):
                        _remove_local_live_input(media_input)
                    self._close_fifos()
                    pipes = None
                    continue
                if start_next_video:
                    continue
            self._persist(
                settings,
                status="stopped",
                stopped_at=_now_iso(),
                audio_player_status="idle",
                audio_position_seconds=0.0,
                audio_duration_seconds=None,
            )
        except Exception as exc:
            logger.exception("live stream failed")
            self._persist(
                settings,
                status="failed",
                stopped_at=_now_iso(),
                last_error=f"{type(exc).__name__}: {exc}",
                audio_player_status="idle",
                audio_position_seconds=0.0,
                audio_duration_seconds=None,
            )
        finally:
            self._terminate_runtime(include_mixer=True)
            self._close_fifos()
            shutil.rmtree(work_dir, ignore_errors=True)
            shutil.rmtree(_preview_dir(settings.work_dir), ignore_errors=True)
            with self._lock:
                self._thread = None
                self._session_id = None
                self._paused = False
                self._pending_playlist = None
                self._switch_requested.clear()
                self._pending_audio_control = None
                self._audio_control_requested.clear()
                self._audio_playback = {
                    "audio_player_status": "idle",
                    "audio_position_seconds": 0.0,
                    "audio_duration_seconds": None,
                    "_position_base_seconds": 0.0,
                    "_started_monotonic": None,
                }
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


def _live_output_config(settings: Any, *, db: Session) -> tuple[dict[str, Any], str]:
    config, target = _stream_target(db)
    auto_profile = get_auto_profile(db)
    config["use_intel_gpu"] = bool(auto_profile.get("use_intel_gpu"))
    if config["use_intel_gpu"]:
        subtitle_settings = get_subtitle_settings()
        config["intel_gpu_render_device"] = (
            str(subtitle_settings.intel_gpu_render_device or "").strip() or "/dev/dri/renderD128"
        )
        try:
            _validate_intel_live_encoder(settings.ffmpeg_path, config["intel_gpu_render_device"])
        except (FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Intel iGPU 直播编码不可用: {exc}。请检查 /dev/dri 映射和 render 组权限，或关闭自动模式中的 Intel iGPU 硬件编码。",
            ) from exc
    return config, target


def _start_live_controller(settings: Any, *, db: Session, config: dict[str, Any], target: str, playlist: dict[str, Any]) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    _update_session(
        db,
        status="starting",
        session_id=session_id,
        started_at=_now_iso(),
        stopped_at=None,
        current_video=None,
        current_audio=None,
        audio_player_status="idle",
        audio_position_seconds=0.0,
        audio_duration_seconds=None,
        audio_playback_mode=str(playlist.get("audio_playback_mode") or "sequential"),
        audio_volume_percent=100,
        mix_audio=bool(playlist.get("mix_audio", False)),
        last_error=None,
    )
    _CONTROLLER.start(settings, session_id=session_id, config=config, target=target, playlist=playlist)
    return get_live_session(db)


def start_live_stream(settings: Any, *, db: Session) -> dict[str, Any]:
    recover_interrupted_live_stream(db)
    if _CONTROLLER.is_active():
        raise HTTPException(status_code=409, detail="已有直播推流正在运行")
    config, target = _live_output_config(settings, db=db)
    playlist = get_live_playlist(db)
    if any(item.get("source") == "task_asset" for item in playlist["video_items"]):
        playlist = update_live_playlist(db, playlist, s3=FileStore(settings))
    video_items = [_normalize_source(item) for item in playlist["video_items"]]
    if not video_items:
        raise HTTPException(status_code=400, detail="请至少选择一个视频资源")
    _validate_playlist_sources(db, video_items, expected_type="video")
    _validate_playlist_sources(db, [_normalize_source(item) for item in playlist["audio_items"]], expected_type="audio")
    return _start_live_controller(settings, db=db, config=config, target=target, playlist=playlist)


def play_live_selection(settings: Any, payload: dict[str, Any], *, db: Session) -> dict[str, Any]:
    recover_interrupted_live_stream(db)
    video = _normalize_source(payload.get("video_item"))
    audio = _normalize_source(payload["audio_item"]) if payload.get("audio_item") else None
    if video.source == "task_asset" or (audio and audio.source == "task_asset"):
        raise HTTPException(status_code=400, detail="请先将任务视频导入直播媒体库，再播放或推流")
    try:
        resolved_video = _resolve_media(db, video, expected_type="video")
        resolved_audio = _resolve_media(db, audio, expected_type="audio") if audio else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    playlist = {
        "video_items": [{"source": video.source, "id": video.id}],
        "audio_items": [{"source": audio.source, "id": audio.id}] if audio else [],
        "playback_mode": "sequential",
        "audio_playback_mode": "sequential",
        "loop_playlist": True,
        "mix_audio": bool(payload.get("mix_audio", False)),
    }
    if _CONTROLLER.is_active():
        _CONTROLLER.switch(playlist)
        return _update_session(
            db,
            status="running",
            current_video={"source": video.source, "id": video.id, "display_name": resolved_video.display_name},
            current_audio=(
                {"source": audio.source, "id": audio.id, "display_name": resolved_audio.display_name} if audio and resolved_audio else None
            ),
            audio_player_status="playing" if audio else "idle",
            audio_position_seconds=0.0,
            audio_duration_seconds=None,
            mix_audio=bool(payload.get("mix_audio", False)),
            last_error=None,
        )
    config, target = _live_output_config(settings, db=db)
    return _start_live_controller(settings, db=db, config=config, target=target, playlist=playlist)


def control_live_audio(payload: dict[str, Any], *, db: Session) -> dict[str, Any]:
    """Validate and queue an audio-only mixer operation.

    The controller processes this between producer polling cycles.  It never
    recreates the video producer, mixer, HLS preview, or RTMP connection.
    """

    session = _session_status(db)
    if session.get("status") != "running" or not _CONTROLLER.is_active():
        raise HTTPException(status_code=409, detail="请先开始直播，再控制独立音频")

    action = str(payload.get("action") or "").strip().lower()
    if action not in {
        "play",
        "pause",
        "resume",
        "previous",
        "next",
        "seek",
        "original",
        "set_playback_mode",
        "set_volume",
    }:
        raise HTTPException(status_code=400, detail="未知的音频播放控制操作")

    try:
        queue = [_normalize_source(item) for item in (payload.get("audio_items") or [])]
        if len(queue) > 1000:
            raise ValueError("音频播放队列最多 1000 首歌曲")
        if any(item.source != "library" for item in queue):
            raise ValueError("直播歌曲必须来自音频媒体库")
        # The saved auto-play list remains capped at 100 items.  This queue is
        # only used for interactive previous/next and may represent the whole
        # audio library, so validate every item without that saved-list cap.
        for item in queue:
            _resolve_media(db, item, expected_type="audio")

        control: dict[str, Any] = {"action": action, "audio_items": [{"source": item.source, "id": item.id} for item in queue]}
        if action == "play":
            selected = _normalize_source(payload.get("audio_item"))
            if selected.source != "library":
                raise ValueError("直播歌曲必须来自音频媒体库")
            _resolve_media(db, selected, expected_type="audio")
            control["audio_item"] = {"source": selected.source, "id": selected.id}
        elif action in {"next", "previous"}:
            if not queue:
                raise ValueError("切换歌曲时缺少歌单队列")
            mode = str(payload.get("playback_mode") or session.get("audio_playback_mode") or "sequential").strip().lower()
            if mode not in {"sequential", "shuffle"}:
                raise ValueError("未知的音频播放模式")
            control["playback_mode"] = mode
        elif action == "set_playback_mode":
            mode = str(payload.get("playback_mode") or "").strip().lower()
            if mode not in {"sequential", "shuffle"}:
                raise ValueError("未知的音频播放模式")
            control["playback_mode"] = mode
            _update_session(db, audio_playback_mode=mode)
        elif action == "set_volume":
            volume = _bounded_int(
                payload.get("volume_percent"),
                default=100,
                minimum=0,
                maximum=200,
                field="独立音频音量",
            )
            control["volume_percent"] = volume
            _update_session(db, audio_volume_percent=volume)
        elif action != "original":
            current = session.get("current_audio")
            if not current:
                raise ValueError("当前没有可控制的独立音频")
            current_source = _normalize_source(current)
            if current_source.source != "library":
                raise ValueError("当前独立音频不可控制")
            _resolve_media(db, current_source, expected_type="audio")
            if action == "seek":
                control["position_seconds"] = float(payload.get("position_seconds") or 0.0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _CONTROLLER.control_audio(control)
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
    return _update_session(
        db,
        status="stopped",
        stopped_at=_now_iso(),
        audio_player_status="idle",
        audio_position_seconds=0.0,
        audio_duration_seconds=None,
    )
