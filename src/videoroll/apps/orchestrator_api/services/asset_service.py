from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from botocore.exceptions import ClientError
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers

from videoroll.apps.orchestrator_api.services.image_validation import (
    validate_and_reencode_cover,
)
from videoroll.apps.subtitle_service.task_title_store import get_task_display_title_with_s3
from videoroll.db.models import AppSetting, Asset, AssetKind, Subtitle, Task, TaskStatus
from videoroll.storage.s3 import S3Store


logger = logging.getLogger(__name__)
PENDING_S3_DELETE_PREFIX = "storage.pending_delete."
UPLOAD_VIDEO_MAX_BYTES = 8 * 1024 * 1024 * 1024
UPLOAD_COVER_MAX_BYTES = 50 * 1024 * 1024


class UploadTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class AssetStreamResult:
    body: Any | None
    media_type: str | None
    headers: dict[str, str]
    status_code: int = 200


_SAFE_CONTENT_TYPES_BY_KIND: dict[AssetKind, frozenset[str]] = {
    AssetKind.video_raw: frozenset(
        {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}
    ),
    AssetKind.video_final: frozenset(
        {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}
    ),
    AssetKind.audio_wav: frozenset({"audio/wav", "audio/x-wav"}),
    AssetKind.cover_image: frozenset({"image/jpeg", "image/png", "image/webp"}),
    AssetKind.metadata_json: frozenset({"application/json"}),
    AssetKind.segments_json: frozenset({"application/json"}),
    AssetKind.publish_result: frozenset({"application/json"}),
    AssetKind.subtitle_srt: frozenset({"application/x-subrip", "text/plain"}),
    AssetKind.subtitle_ass: frozenset({"text/plain"}),
    AssetKind.log: frozenset({"text/plain"}),
}
_INLINE_ASSET_KINDS = frozenset(
    {AssetKind.video_raw, AssetKind.video_final, AssetKind.audio_wav}
)


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def clean_download_filename(name: str, *, max_len: int = 120) -> str:
    cleaned = str(name or "").replace("\r", " ").replace("\n", " ").strip()
    cleaned = cleaned.replace("/", " ").replace("\\", " ").replace('"', "'")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return "download.bin"
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1] + "…"
    return cleaned


def content_disposition(filename: str, *, inline: bool) -> str:
    disposition = "inline" if inline else "attachment"
    cleaned = clean_download_filename(filename)
    fallback = "".join(char if 32 <= ord(char) < 127 else "_" for char in cleaned)
    fallback = fallback.replace("\\", "_").replace('"', "_").strip() or "download.bin"
    encoded = quote(cleaned, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def safe_asset_content_type(asset: Asset, content_type: str | None) -> str:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    allowed = _SAFE_CONTENT_TYPES_BY_KIND.get(asset.kind, frozenset())
    return normalized if normalized in allowed else "application/octet-stream"


def safe_asset_headers(
    asset: Asset,
    content_type: str | None,
    inline: bool,
    *,
    filename: str | None = None,
) -> dict[str, str]:
    safe_type = safe_asset_content_type(asset, content_type)
    allow_inline = (
        inline
        and asset.kind in _INLINE_ASSET_KINDS
        and safe_type != "application/octet-stream"
    )
    return {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": content_disposition(
            filename or Path(asset.storage_key).name or "download.bin",
            inline=allow_inline,
        ),
    }


def suggest_asset_filename(db: Session, task_id: uuid.UUID, asset: Asset, *, s3: S3Store | None) -> str:
    base = Path(asset.storage_key).name or "download.bin"
    if asset.kind == AssetKind.video_final:
        title = get_task_display_title_with_s3(db, str(task_id), s3=s3).strip()
        if title:
            extension = Path(base).suffix
            return f"{title}{extension}" if extension and len(extension) <= 8 else title
    return base


def get_task(db: Session, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def list_task_assets(db: Session, task_id: uuid.UUID) -> list[Asset]:
    get_task(db, task_id)
    return db.query(Asset).filter(Asset.task_id == task_id).order_by(Asset.created_at.asc()).all()


def get_task_asset(db: Session, task_id: uuid.UUID, asset_id: uuid.UUID) -> Asset:
    asset = db.get(Asset, asset_id)
    if not asset or asset.task_id != task_id:
        raise HTTPException(status_code=404, detail="asset not found")
    return asset


def parse_range_header(range_header: str, total_size: int) -> tuple[int, int] | None:
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
        return max(0, total_size - suffix_len), total_size - 1
    if "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        start = int(start_text)
    except Exception:
        return None
    if start < 0:
        return None
    if end_text.strip() == "":
        end = total_size - 1
    else:
        try:
            end = int(end_text)
        except Exception:
            return None
    if end < start or start >= total_size:
        return None
    return start, min(end, total_size - 1)


def read_s3_bytes(s3: S3Store, key: str) -> bytes:
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


def read_s3_json_object(s3: S3Store, key: str) -> dict[str, Any] | None:
    try:
        raw = read_s3_bytes(s3, key)
    except ClientError as exc:
        if is_s3_object_missing(exc):
            return None
        raise
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def is_s3_object_missing(exc: ClientError) -> bool:
    code = str((as_dict(exc.response.get("Error")).get("Code") or "")).strip()
    return code in {"NoSuchKey", "404", "NotFound"}


def write_s3_json(s3: S3Store, key: str, value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_bytes(payload, key, content_type="application/json")
    return payload


def write_s3_text(s3: S3Store, key: str, value: str) -> bytes:
    payload = str(value).encode("utf-8")
    s3.put_bytes(payload, key, content_type="text/plain; charset=utf-8")
    return payload


def pending_s3_delete_key(storage_key: str) -> str:
    digest = hashlib.sha256(str(storage_key).encode("utf-8")).hexdigest()
    return f"{PENDING_S3_DELETE_PREFIX}{digest}"


def queue_pending_s3_delete(
    db: Session,
    storage_key: str,
    *,
    reason: str,
    commit: bool = True,
) -> AppSetting:
    row_key = pending_s3_delete_key(storage_key)
    row = db.get(AppSetting, row_key)
    if row is None:
        row = AppSetting(key=row_key, value_json={})
    row.value_json = {
        "storage_key": str(storage_key),
        "reason": str(reason),
        "requested_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    db.add(row)
    if commit:
        db.commit()
    return row


def storage_key_is_referenced(db: Session, storage_key: str) -> bool:
    return bool(
        db.query(Asset.id).filter(Asset.storage_key == storage_key).first()
        or db.query(Subtitle.id).filter(Subtitle.storage_key == storage_key).first()
    )


def retry_pending_s3_deletes(db: Session, s3: S3Store, *, limit: int = 200) -> int:
    rows = (
        db.query(AppSetting)
        .filter(AppSetting.key.like(f"{PENDING_S3_DELETE_PREFIX}%"))
        .order_by(AppSetting.key.asc())
        .limit(max(1, int(limit)))
        .all()
    )
    deleted = 0
    for row in rows:
        storage_key = str((row.value_json or {}).get("storage_key") or "").strip()
        if not storage_key:
            db.delete(row)
            continue
        if storage_key_is_referenced(db, storage_key):
            db.delete(row)
            continue
        try:
            s3.delete_object(storage_key)
        except Exception:
            logger.warning("pending S3 delete retry failed", extra={"storage_key": storage_key})
            continue
        db.delete(row)
        deleted += 1
    db.commit()
    return deleted


def prepare_asset_download(
    db: Session,
    s3: S3Store,
    *,
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> AssetStreamResult:
    asset = get_task_asset(db, task_id, asset_id)
    try:
        response = s3.get_object(asset.storage_key)
    except ClientError as exc:
        if is_s3_object_missing(exc):
            raise HTTPException(status_code=404, detail="asset object not found") from exc
        raise
    filename = suggest_asset_filename(db, task_id, asset, s3=s3)
    content_type = response.get("ContentType") or "application/octet-stream"
    headers = safe_asset_headers(asset, content_type, False, filename=filename)
    length = response.get("ContentLength") or asset.size_bytes
    if isinstance(length, int):
        headers["Content-Length"] = str(length)
    return AssetStreamResult(
        body=response["Body"],
        media_type=safe_asset_content_type(asset, content_type),
        headers=headers,
    )


def prepare_asset_stream(
    db: Session,
    s3: S3Store,
    *,
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    range_header: str,
) -> AssetStreamResult:
    asset = get_task_asset(db, task_id, asset_id)
    filename = suggest_asset_filename(db, task_id, asset, s3=s3)
    total_size: int | None = None
    stored_content_type = "application/octet-stream"
    try:
        head = s3.head_object(asset.storage_key)
        if isinstance(head.get("ContentLength"), int):
            total_size = int(head["ContentLength"])
        if head.get("ContentType"):
            stored_content_type = str(head["ContentType"]) or stored_content_type
    except Exception:
        total_size = asset.size_bytes if isinstance(asset.size_bytes, int) else None
    base_headers = {
        "Accept-Ranges": "bytes",
        **safe_asset_headers(asset, stored_content_type, True, filename=filename),
    }
    if range_header and isinstance(total_size, int) and total_size > 0:
        parsed = parse_range_header(range_header, total_size)
        if not parsed:
            return AssetStreamResult(
                body=None,
                media_type=None,
                headers={**base_headers, "Content-Range": f"bytes */{total_size}"},
                status_code=416,
            )
        start, end = parsed
        try:
            response = s3.get_object(asset.storage_key, range_bytes=f"bytes={start}-{end}")
        except ClientError as exc:
            if is_s3_object_missing(exc):
                raise HTTPException(status_code=404, detail="asset object not found") from exc
            raise
        return AssetStreamResult(
            body=response["Body"],
            media_type=safe_asset_content_type(asset, stored_content_type),
            headers={
                **base_headers,
                "Content-Range": f"bytes {start}-{end}/{total_size}",
                "Content-Length": str(end - start + 1),
            },
            status_code=206,
        )
    try:
        response = s3.get_object(asset.storage_key)
    except ClientError as exc:
        if is_s3_object_missing(exc):
            raise HTTPException(status_code=404, detail="asset object not found") from exc
        raise
    response_content_type = response.get("ContentType") or stored_content_type
    headers = {
        "Accept-Ranges": "bytes",
        **safe_asset_headers(asset, response_content_type, True, filename=filename),
    }
    length = response.get("ContentLength") or asset.size_bytes
    if isinstance(length, int):
        headers["Content-Length"] = str(length)
    return AssetStreamResult(
        body=response["Body"],
        media_type=safe_asset_content_type(asset, response_content_type),
        headers=headers,
    )


def stream_upload_to_tempfile(
    file_obj: Any,
    *,
    prefix: str,
    suffix: str,
    max_bytes: int | None = None,
) -> tuple[Path, str, int]:
    temp_path: Path | None = None
    digest = hashlib.sha256()
    size_bytes = 0
    limit = int(max_bytes or 0)
    try:
        with tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise TypeError("uploaded file stream returned non-bytes content")
                digest.update(chunk)
                size_bytes += len(chunk)
                if limit > 0 and size_bytes > limit:
                    raise UploadTooLargeError(f"upload too large: max {limit} bytes")
                temp_file.write(chunk)
        return temp_path, digest.hexdigest(), size_bytes
    except Exception:
        safe_unlink(temp_path)
        raise


def safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


async def store_uploaded_task_asset(
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
    max_bytes: int | None = None,
) -> Asset:
    suffix = Path(file.filename or "").suffix or default_suffix
    temp_path: Path | None = None
    uploaded_key: str | None = None
    try:
        # Keep all blocking file operations behind the service's injectable
        # thread-pool boundary.  Starlette's UploadFile delegates disk-backed
        # seeks/closes to its own pool, which makes canonical cover uploads
        # difficult to control and test consistently.
        await run_in_threadpool(file.file.seek, 0)
        temp_path, sha256, size_bytes = await run_in_threadpool(
            stream_upload_to_tempfile,
            file.file,
            prefix=temp_prefix,
            suffix=suffix,
            max_bytes=max_bytes,
        )
        uploaded_key = (
            f"{key_prefix}/{task.id}/{object_name_prefix}_{sha256[:16]}_{uuid.uuid4().hex[:12]}{suffix}"
        )
        await run_in_threadpool(s3.upload_file, temp_path, uploaded_key, file.content_type or None)
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
    except UploadTooLargeError as exc:
        db.rollback()
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        if uploaded_key:
            try:
                queue_pending_s3_delete(db, uploaded_key, reason="failed_asset_upload")
            except Exception:
                db.rollback()
                logger.exception("failed to queue uploaded S3 object cleanup", extra={"storage_key": uploaded_key})
        raise HTTPException(status_code=500, detail=f"upload failed: {exc}") from exc
    finally:
        await run_in_threadpool(safe_unlink, temp_path)
        try:
            await run_in_threadpool(file.file.close)
        except Exception:
            pass


async def upload_task_video(
    task_id: uuid.UUID,
    file: UploadFile,
    *,
    db: Session,
    s3: S3Store,
) -> Asset:
    task = get_task(db, task_id)
    if file.content_type and not (
        file.content_type.startswith("video/") or file.content_type == "application/octet-stream"
    ):
        raise HTTPException(status_code=400, detail="video upload must be a video file")
    return await store_uploaded_task_asset(
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
        max_bytes=UPLOAD_VIDEO_MAX_BYTES,
    )


async def upload_task_cover(
    task_id: uuid.UUID,
    file: UploadFile,
    *,
    db: Session,
    s3: S3Store,
) -> Asset:
    task = get_task(db, task_id)
    try:
        await run_in_threadpool(file.file.seek, 0)
        validated = await run_in_threadpool(validate_and_reencode_cover, file.file)
    finally:
        try:
            await run_in_threadpool(file.file.close)
        except Exception:
            pass
    canonical_file = tempfile.TemporaryFile()
    canonical_file.write(validated.data)
    canonical_file.seek(0)
    canonical_upload = UploadFile(
        canonical_file,
        filename=f"cover{validated.extension}",
        headers=Headers({"content-type": validated.content_type}),
    )
    return await store_uploaded_task_asset(
        task=task,
        file=canonical_upload,
        s3=s3,
        db=db,
        temp_prefix="videoroll_cover_",
        default_suffix=".jpg",
        key_prefix="final",
        object_name_prefix="cover",
        asset_kind=AssetKind.cover_image,
        max_bytes=UPLOAD_COVER_MAX_BYTES,
    )


def delete_final_asset(
    *,
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    db: Session,
    s3: S3Store,
) -> dict[str, bool]:
    asset = db.get(Asset, asset_id)
    if not asset or asset.task_id != task_id:
        raise HTTPException(status_code=404, detail="asset not found")
    if asset.kind != AssetKind.video_final:
        raise HTTPException(status_code=400, detail="only video_final assets can be deleted")

    storage_key = asset.storage_key
    try:
        queue_pending_s3_delete(
            db,
            storage_key,
            reason="manual_asset_delete",
            commit=False,
        )
        db.query(Subtitle).filter(
            Subtitle.task_id == task_id,
            Subtitle.storage_key == storage_key,
        ).delete(synchronize_session=False)
        db.delete(asset)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"deleted": True}
