from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException

from videoroll.apps.orchestrator_api import main as orchestrator_main
from videoroll.apps.subtitle_service import main as subtitle_main


def test_stream_upload_to_tempfile_rejects_oversize_and_removes_temp(tmp_path, monkeypatch) -> None:
    real_named_temporary_file = orchestrator_main.tempfile.NamedTemporaryFile

    def named_temporary_file(*args: object, **kwargs: object):
        kwargs["dir"] = tmp_path
        return real_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(orchestrator_main.tempfile, "NamedTemporaryFile", named_temporary_file)

    with pytest.raises(orchestrator_main.UploadTooLargeError):
        orchestrator_main._stream_upload_to_tempfile(
            io.BytesIO(b"abcdef"),
            prefix="upload_",
            suffix=".bin",
            max_bytes=3,
        )

    assert list(tmp_path.iterdir()) == []


def test_safe_extract_zip_extracts_via_temp_dir(tmp_path) -> None:
    zip_path = tmp_path / "model.zip"
    dest = tmp_path / "model"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("config.json", "{}")
        zf.writestr("weights/model.bin", b"123")

    subtitle_main._safe_extract_zip(zip_path, dest, max_files=10, max_uncompressed_bytes=100)

    assert (dest / "config.json").read_text(encoding="utf-8") == "{}"
    assert (dest / "weights" / "model.bin").read_bytes() == b"123"
    assert not list(tmp_path.glob(".model.extract-*"))


def test_safe_extract_zip_rejects_too_many_files_without_partial_dest(tmp_path) -> None:
    zip_path = tmp_path / "model.zip"
    dest = tmp_path / "model"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("a.txt", "a")
        zf.writestr("b.txt", "b")

    with pytest.raises(HTTPException) as exc:
        subtitle_main._safe_extract_zip(zip_path, dest, max_files=1, max_uncompressed_bytes=100)

    assert exc.value.status_code == 413
    assert not dest.exists()
    assert not list(tmp_path.glob(".model.extract-*"))


def test_safe_extract_zip_rejects_zip_bomb_size_without_partial_dest(tmp_path) -> None:
    zip_path = tmp_path / "model.zip"
    dest = tmp_path / "model"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("large.bin", b"x" * 8)

    with pytest.raises(HTTPException) as exc:
        subtitle_main._safe_extract_zip(zip_path, dest, max_files=10, max_uncompressed_bytes=4)

    assert exc.value.status_code == 413
    assert not dest.exists()
    assert not list(tmp_path.glob(".model.extract-*"))


def test_safe_extract_zip_rejects_unsafe_paths_without_partial_dest(tmp_path) -> None:
    zip_path = tmp_path / "model.zip"
    dest = tmp_path / "model"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escape.txt", "bad")

    with pytest.raises(HTTPException) as exc:
        subtitle_main._safe_extract_zip(zip_path, dest, max_files=10, max_uncompressed_bytes=100)

    assert exc.value.status_code == 400
    assert not dest.exists()
    assert not list(tmp_path.glob(".model.extract-*"))


def test_safe_extract_zip_rejects_backslash_traversal_without_partial_dest(tmp_path) -> None:
    zip_path = tmp_path / "model.zip"
    dest = tmp_path / "model"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("nested\\..\\escape.txt", "bad")

    with pytest.raises(HTTPException) as exc:
        subtitle_main._safe_extract_zip(zip_path, dest, max_files=10, max_uncompressed_bytes=100)

    assert exc.value.status_code == 400
    assert not dest.exists()
    assert not list(tmp_path.glob(".model.extract-*"))
