from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from videoroll.storage.filesystem import FileStore, StorageObjectNotFound


def _store(tmp_path: Path) -> FileStore:
    store = FileStore(SimpleNamespace(storage_root=str(tmp_path / "objects")))
    store.ensure_ready()
    return store


@pytest.mark.parametrize("key", ["", "/absolute", "../escape", "raw/../../escape", "raw\\escape"])
def test_storage_keys_cannot_escape_root(tmp_path: Path, key: str) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError):
        store.path_for(key, require_exists=False)


def test_put_and_range_stream(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_bytes(b"0123456789", "raw/task/video.bin")

    result = store.get_object("raw/task/video.bin", range_bytes="bytes=2-5")
    body = result["Body"]
    try:
        assert body.read() == b"2345"
    finally:
        body.close()
    assert result["ContentLength"] == 4
    assert result["ContentRange"] == "bytes 2-5/10"


def test_promote_moves_completed_work_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = tmp_path / "work" / "final.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")

    store.promote_file(source, "final/task/video.mp4")

    assert not source.exists()
    assert store.path_for("final/task/video.mp4").read_bytes() == b"video"


def test_live_copy_uses_hardlink_without_coupling_deletion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_bytes(b"large-video", "final/task/video.mp4")
    store.copy_object("final/task/video.mp4", "live/video/item/video.mp4")

    source = store.path_for("final/task/video.mp4")
    imported = store.path_for("live/video/item/video.mp4")
    assert os.path.samefile(source, imported)

    store.delete_object("final/task/video.mp4")
    with pytest.raises(StorageObjectNotFound):
        store.path_for("final/task/video.mp4")
    assert imported.read_bytes() == b"large-video"


def test_cleanup_partials_removes_only_stale_partial_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stale = store.partial_root / "stale.partial"
    fresh = store.partial_root / "fresh.partial"
    unrelated = store.partial_root / "keep.bin"
    for path in (stale, fresh, unrelated):
        path.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    assert store.cleanup_partials(older_than_seconds=300) == 1
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()
