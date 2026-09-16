from __future__ import annotations

import hashlib
from pathlib import Path
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.services import playout_service
from videoroll.apps.orchestrator_api.dependencies import get_db, get_s3, get_settings
from videoroll.apps.orchestrator_api.routers.assets import router as assets_router
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, PlayoutAssetLink, SourceLicense, SourceType, Task, TaskStatus
from videoroll.storage.filesystem import FileStore


def _storage(tmp_path: Path) -> tuple[FileStore, Path]:
    root = tmp_path / "objects"
    store = FileStore(type("Settings", (), {"storage_root": str(root)})())
    store.ensure_ready()
    return store, root


def _playout_root(tmp_path: Path) -> Path:
    # Deliberately unrelated to STORAGE_ROOT so the test catches accidental
    # derivation of the ffplayout media root from the object-store path.
    return tmp_path / "dedicated-playout-media"


def _task_and_asset(db: Session, root: Path, *, kind: AssetKind = AssetKind.video_final, key: str = "final/video.mp4") -> tuple[Task, Asset, Path]:
    task = Task(source_type=SourceType.local, source_license=SourceLicense.own, status=TaskStatus.rendered)
    db.add(task)
    db.flush()
    asset = Asset(task_id=task.id, kind=kind, storage_key=key)
    db.add(asset)
    db.flush()
    source = root / key
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"final-video-content")
    asset.sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    asset.size_bytes = source.stat().st_size
    db.commit()
    return task, asset, source


@pytest.fixture
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    Base.metadata.create_all(engine, tables=[Task.__table__, Asset.__table__, PlayoutAssetLink.__table__])
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_import_uses_hardlink_and_is_idempotent(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, source = _task_and_asset(db, root)

    first = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    second = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))

    destination = _playout_root(tmp_path) / first["playout_path"]
    assert first["status"] == "ready"
    assert first["transfer_mode"] == "hardlink"
    assert first["task_id"] == task.id
    assert first["asset_id"] == asset.id
    assert destination.is_file()
    assert destination.samefile(source)
    assert second["id"] == first["id"]
    assert db.scalar(select(PlayoutAssetLink.id).where(PlayoutAssetLink.asset_id == asset.id)) == first["id"]


def test_import_falls_back_to_copy_across_filesystems(db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage, root = _storage(tmp_path)
    task, asset, source = _task_and_asset(db, root)

    def fail_link(*_args, **_kwargs):
        raise OSError("cross-device link")

    monkeypatch.setattr(playout_service.os, "link", fail_link)
    result = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    destination = _playout_root(tmp_path) / result["playout_path"]

    assert result["transfer_mode"] == "copy"
    assert destination.read_bytes() == source.read_bytes()
    assert not destination.samefile(source)


def test_import_rejects_non_final_assets_and_path_escape(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, raw_asset, _ = _task_and_asset(db, root, kind=AssetKind.video_raw, key="raw/video.mp4")

    with pytest.raises(HTTPException) as raw_error:
        playout_service.import_asset_to_playout(task.id, raw_asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert raw_error.value.status_code == 400

    final_task, bad_asset, _ = _task_and_asset(db, root, key="../outside.mp4")
    with pytest.raises(HTTPException) as path_error:
        playout_service.import_asset_to_playout(final_task.id, bad_asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert path_error.value.status_code == 409


def test_import_requires_asset_to_belong_to_task(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, _ = _task_and_asset(db, root)
    other_task = Task(source_type=SourceType.local, source_license=SourceLicense.own, status=TaskStatus.rendered)
    db.add(other_task)
    db.commit()

    with pytest.raises(HTTPException) as error:
        playout_service.import_asset_to_playout(other_task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert error.value.status_code == 404


def test_remove_asset_link_keeps_the_destination_and_deletes_only_mapping(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, _ = _task_and_asset(db, root)
    result = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    destination = _playout_root(tmp_path) / result["playout_path"]
    assert destination.is_file()

    playout_service.remove_asset_link(asset.id, db=db, storage=storage)
    db.commit()

    assert destination.is_file()
    assert db.scalar(select(PlayoutAssetLink.id).where(PlayoutAssetLink.asset_id == asset.id)) is None


def test_playout_media_survives_source_asset_unlink(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, source = _task_and_asset(db, root)
    result = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    destination = _playout_root(tmp_path) / result["playout_path"]

    source.unlink()

    assert not source.exists()
    assert destination.is_file()
    assert destination.read_bytes() == b"final-video-content"


def test_repeated_import_rejects_a_corrupted_copy_destination(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, root = _storage(tmp_path)
    task, asset, _ = _task_and_asset(db, root)

    def fail_link(*_args, **_kwargs):
        raise OSError("EXDEV")

    monkeypatch.setattr(playout_service.os, "link", fail_link)
    result = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    destination = _playout_root(tmp_path) / result["playout_path"]
    destination.write_bytes(b"corrupted")

    with pytest.raises(HTTPException) as error:
        playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert error.value.status_code == 409


def test_playout_import_route_returns_the_id_only_contract(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, _ = _task_and_asset(db, root)
    application = FastAPI()
    application.include_router(assets_router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_s3] = lambda: storage
    application.dependency_overrides[get_settings] = lambda: type(
        "Settings",
        (),
        {"playout_media_root": str(_playout_root(tmp_path))},
    )()

    with TestClient(application) as client:
        response = client.post(
            f"/tasks/{task.id}/assets/{asset.id}/playout",
            json={"source": "/etc/passwd"},
        )
        listing = client.get(f"/tasks/{task.id}/playout-assets")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["task_id"] == str(task.id)
    assert payload["asset_id"] == str(asset.id)
    assert payload["playout_path"].startswith(f"VideoRoll/{task.id}/")
    assert listing.status_code == 200
    assert len(listing.json()) == 1
