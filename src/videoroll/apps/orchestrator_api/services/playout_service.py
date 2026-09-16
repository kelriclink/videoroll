from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.db.models import Asset, AssetKind, PlayoutAssetLink, Task
from videoroll.storage.filesystem import FileStore, StorageObjectNotFound


logger = logging.getLogger(__name__)
DEFAULT_FFPLAYOUT_CHANNEL_ID = 1
PLAYOUT_MEDIA_DIRECTORY = "VideoRoll"


def _task_or_404(db: Session, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _final_asset_or_404(db: Session, task_id: uuid.UUID, asset_id: uuid.UUID) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.task_id != task_id:
        raise HTTPException(status_code=404, detail="asset not found")
    if asset.kind != AssetKind.video_final:
        raise HTTPException(status_code=400, detail="only video_final assets can be added to playout")
    return asset


def _safe_filename(storage_key: str, asset_id: uuid.UUID) -> str:
    name = PurePosixPath(str(storage_key)).name
    cleaned = "".join(
        "_" if char in {"/", "\\"} or ord(char) < 32 else char
        for char in name.replace("\x00", "")
    ).strip()
    if not cleaned or cleaned in {".", ".."}:
        cleaned = f"video-{asset_id}.mp4"
    return cleaned[:180]


def _relative_media_path(task_id: uuid.UUID, storage_key: str, asset_id: uuid.UUID) -> str:
    return (
        PurePosixPath(PLAYOUT_MEDIA_DIRECTORY)
        / str(task_id)
        / _safe_filename(storage_key, asset_id)
    ).as_posix()


def _resolve_media_path(root: Path, relative_path: str) -> Path:
    raw = str(relative_path or "").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise HTTPException(status_code=500, detail="stored playout path is invalid")

    resolved_root = root.expanduser().resolve()
    resolved = resolved_root.joinpath(*path.parts).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="stored playout path escapes media root") from exc
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_checksum(asset: Asset, source: Path) -> str:
    declared = str(asset.sha256 or "").strip().lower()
    if len(declared) == 64:
        try:
            int(declared, 16)
        except ValueError:
            pass
        else:
            return declared
    return _sha256_file(source)


def _same_file(source: Path, destination: Path) -> bool:
    try:
        return os.path.samefile(source, destination)
    except (FileNotFoundError, OSError):
        return False


def _existing_destination_mode(source: Path, destination: Path, checksum: str) -> str | None:
    if not destination.exists():
        return None
    if not destination.is_file():
        raise HTTPException(status_code=409, detail="playout destination is not a file")
    if _same_file(source, destination):
        return "hardlink"
    if _sha256_file(destination) == checksum:
        return "copy"
    raise HTTPException(status_code=409, detail="playout destination already contains different content")


def _transfer(source: Path, destination: Path, checksum: str) -> str:
    existing_mode = _existing_destination_mode(source, destination, checksum)
    if existing_mode:
        return existing_mode

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    transfer_mode = "copy"
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".partial",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        temporary_path.unlink()

        try:
            os.link(source, temporary_path)
            transfer_mode = "hardlink"
        except OSError:
            shutil.copy2(source, temporary_path)
            transfer_mode = "copy"
        os.replace(temporary_path, destination)
        temporary_path = None
        return transfer_mode
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _link_response(link: PlayoutAssetLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "status": "ready",
        "task_id": link.task_id,
        "asset_id": link.asset_id,
        "ffplayout_channel_id": link.ffplayout_channel_id,
        "playout_path": link.relative_media_path,
        "relative_media_path": link.relative_media_path,
        "transfer_mode": link.transfer_mode,
        "source_checksum": link.source_checksum,
        "created_at": link.created_at,
        "updated_at": link.updated_at,
    }


def list_task_playout_links(db: Session, task_id: uuid.UUID) -> list[dict[str, Any]]:
    _task_or_404(db, task_id)
    links = (
        db.query(PlayoutAssetLink)
        .filter(PlayoutAssetLink.task_id == task_id)
        .order_by(PlayoutAssetLink.created_at.asc())
        .all()
    )
    return [_link_response(link) for link in links]


def remove_asset_link(asset_id: uuid.UUID, *, db: Session, storage: FileStore) -> None:
    link = (
        db.query(PlayoutAssetLink)
        .filter(PlayoutAssetLink.asset_id == asset_id)
        .one_or_none()
    )
    # Keeping this guard makes the helper harmless for compatibility callers
    # that use a lightweight/mock session rather than a SQLAlchemy Session.
    if not isinstance(link, PlayoutAssetLink):
        return
    # The file under playout-media belongs to ffplayout after import.  Removing
    # the VideoRoll mapping must never invalidate an existing ffplayout
    # playlist entry; media deletion is a separate ffplayout operation.
    _ = storage
    db.delete(link)


def import_asset_to_playout(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    *,
    db: Session,
    storage: FileStore,
    media_root: Path,
    channel_id: int = DEFAULT_FFPLAYOUT_CHANNEL_ID,
) -> dict[str, Any]:
    task = _task_or_404(db, task_id)
    asset = _final_asset_or_404(db, task.id, asset_id)
    try:
        source = storage.path_for(asset.storage_key)
    except StorageObjectNotFound as exc:
        raise HTTPException(status_code=404, detail="asset object not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="asset storage key is invalid") from exc

    checksum = _source_checksum(asset, source)
    relative_path = _relative_media_path(task.id, asset.storage_key, asset.id)
    destination = _resolve_media_path(Path(media_root), relative_path)

    existing = (
        db.query(PlayoutAssetLink)
        .filter(PlayoutAssetLink.asset_id == asset.id)
        .one_or_none()
    )
    if existing is not None:
        if existing.source_checksum != checksum:
            raise HTTPException(status_code=409, detail="source asset checksum changed")
        if existing.relative_media_path != relative_path or existing.ffplayout_channel_id != channel_id:
            raise HTTPException(status_code=409, detail="asset is already linked to another playout destination")
        _transfer(source, destination, checksum)
        return _link_response(existing)

    transfer_mode = _transfer(source, destination, checksum)
    link = PlayoutAssetLink(
        task_id=task.id,
        asset_id=asset.id,
        ffplayout_channel_id=channel_id,
        relative_media_path=relative_path,
        transfer_mode=transfer_mode,
        source_checksum=checksum,
    )
    db.add(link)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.query(PlayoutAssetLink).filter(PlayoutAssetLink.asset_id == asset.id).one_or_none()
        if winner is not None and winner.source_checksum == checksum:
            return _link_response(winner)
        logger.exception(
            "failed to persist playout asset link",
            extra={"task_id": str(task.id), "asset_id": str(asset.id)},
        )
        raise HTTPException(status_code=409, detail="asset could not be linked to playout") from None
    db.refresh(link)
    return _link_response(link)
