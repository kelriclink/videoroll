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
from videoroll.apps.orchestrator_api.dependencies import get_db, get_store, get_settings
from videoroll.apps.orchestrator_api.routers.assets import router as assets_router
from videoroll.db.base import Base
from videoroll.db.models import AppSetting, Asset, AssetKind, PlayoutAssetLink, SourceLicense, SourceType, Task, TaskStatus
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
    Base.metadata.create_all(engine, tables=[Task.__table__, Asset.__table__, PlayoutAssetLink.__table__, AppSetting.__table__])
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
    assert first["playout_path"] == "VideoRoll/video.mp4"
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
    application.dependency_overrides[get_store] = lambda: storage
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
    assert payload["playout_path"] == "VideoRoll/video.mp4"
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_import_uses_translated_chinese_title_at_video_roll_root(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, _ = _task_and_asset(db, root)
    db.add(AppSetting(key=f"task.title.{task.id}", value_json={
        "source_title": "Original title", "translated_title": "为什么电路板上的走线粗细不同？"
    }))
    db.commit()
    first = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    second = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert first["playout_path"] == "VideoRoll/为什么电路板上的走线粗细不同？.mp4"
    assert second["playout_path"] == first["playout_path"]
    assert (_playout_root(tmp_path) / first["playout_path"]).is_file()


def test_title_filename_sanitized_no_subfolders(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, _ = _task_and_asset(db, root)
    db.add(AppSetting(key=f"task.title.{task.id}", value_json={"translated_title": "  中文/标题\n:第一期?*  "}))
    db.commit()
    result = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert result["playout_path"].startswith("VideoRoll/中文_标题")
    assert len(Path(result["playout_path"]).parts) == 2
    assert Path(result["playout_path"]).suffix == ".mp4"


def test_two_assets_with_same_title_get_distinct_flat_names(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    t1, a1, _ = _task_and_asset(db, root, key="final/first.mp4")
    t2, a2, _ = _task_and_asset(db, root, key="final/second.mp4")
    db.add_all([
        AppSetting(key=f"task.title.{t1.id}", value_json={"translated_title": "同名测试"}),
        AppSetting(key=f"task.title.{t2.id}", value_json={"translated_title": "同名测试"}),
    ])
    db.commit()
    p1 = playout_service.import_asset_to_playout(t1.id, a1.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    p2 = playout_service.import_asset_to_playout(t2.id, a2.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert p1["playout_path"] == "VideoRoll/同名测试.mp4"
    assert p2["playout_path"].startswith("VideoRoll/同名测试-")
    assert p1["playout_path"] != p2["playout_path"]
    assert (_playout_root(tmp_path) / p1["playout_path"]).is_file()
    assert (_playout_root(tmp_path) / p2["playout_path"]).is_file()


def test_existing_nested_playout_link_kept_for_playlists(db: Session, tmp_path: Path) -> None:
    storage, root = _storage(tmp_path)
    task, asset, source = _task_and_asset(db, root)
    old_path = f"VideoRoll/{task.id}/video.mp4"
    dest = _playout_root(tmp_path) / old_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.hardlink_to(source)
    db.add(PlayoutAssetLink(task_id=task.id, asset_id=asset.id, ffplayout_channel_id=1,
                             relative_media_path=old_path, transfer_mode="hardlink", source_checksum=asset.sha256))
    db.commit()
    result = playout_service.import_asset_to_playout(task.id, asset.id, db=db, storage=storage, media_root=_playout_root(tmp_path))
    assert result["playout_path"] == old_path
    assert dest.samefile(source)


def test_chinese_filename_is_byte_bounded_and_sanitizes_windows_separators() -> None:
    title = "测试" * 120 + '/\\:*?"<>|'
    name = playout_service._safe_media_filename(title, "video/final.mp4", uuid.uuid4())
    assert name.endswith(".mp4")
    assert len(name.encode("utf-8")) <= 180
    assert not any(c in name for c in '/\\:*?"<>|')
    assert name.startswith("测试测试")


def test_exclusive_copy_race_preserves_another_importer_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch
    import errno

    source = tmp_path / "source.mp4"
    source.write_bytes(b"this video is the source")
    destination = tmp_path / "other-video.mp4"
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    original_open = Path.open

    def inject_race(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path == destination and mode == "xb":
            # A concurrent writer created a different video immediately before
            # the exclusive fallback destination was opened.
            destination.write_bytes(b"different concurrent video")
            raise FileExistsError(errno.EEXIST, "already exists", str(destination))
        return original_open(path, mode, *args, **kwargs)

    original_link = playout_service.os.link

    def no_links(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hard links unsupported")

    monkeypatch.setattr(playout_service.os, "link", no_links)
    with patch.object(Path, "open", inject_race):
        with pytest.raises(FileExistsError):
            playout_service._transfer(source, destination, checksum)
    assert destination.read_bytes() == b"different concurrent video"
    monkeypatch.setattr(playout_service.os, "link", original_link)
