from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tempfile
import uuid
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from PIL import Image, PngImagePlugin
from starlette.datastructures import Headers, UploadFile

from videoroll.apps.orchestrator_api.services import asset_service
from videoroll.db.models import AssetKind


def _image_bytes(
    image_format: str,
    *,
    size: tuple[int, int] = (3, 2),
    metadata: bool = False,
) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", size, color=(10, 20, 30))
    save_options: dict[str, object] = {}
    if metadata and image_format == "JPEG":
        exif = Image.Exif()
        exif[0x010E] = "private description"
        save_options["exif"] = exif
    if metadata and image_format == "PNG":
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("private", "secret metadata")
        save_options["pnginfo"] = pnginfo
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def test_svg_cover_is_rejected() -> None:
    from videoroll.apps.orchestrator_api.services.image_validation import (
        validate_and_reencode_cover,
    )

    with pytest.raises(HTTPException):
        validate_and_reencode_cover(io.BytesIO(b"<svg/onload=alert(1)>"))


@pytest.mark.parametrize(
    ("image_format", "expected_type", "expected_extension", "magic"),
    [
        ("JPEG", "image/jpeg", ".jpg", b"\xff\xd8\xff"),
        ("PNG", "image/png", ".png", b"\x89PNG\r\n\x1a\n"),
        ("WEBP", "image/webp", ".webp", b"RIFF"),
    ],
)
def test_raster_cover_is_decoded_and_reencoded_canonically(
    image_format: str,
    expected_type: str,
    expected_extension: str,
    magic: bytes,
) -> None:
    from videoroll.apps.orchestrator_api.services.image_validation import (
        validate_and_reencode_cover,
    )

    result = validate_and_reencode_cover(io.BytesIO(_image_bytes(image_format)))

    assert result.content_type == expected_type
    assert result.extension == expected_extension
    assert result.data.startswith(magic)
    assert result.width == 3
    assert result.height == 2


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_cover_reencoding_strips_metadata(image_format: str) -> None:
    from videoroll.apps.orchestrator_api.services.image_validation import (
        validate_and_reencode_cover,
    )

    result = validate_and_reencode_cover(
        io.BytesIO(_image_bytes(image_format, metadata=True))
    )

    with Image.open(io.BytesIO(result.data)) as decoded:
        assert decoded.getexif() == {}
        assert "private" not in decoded.info


def test_cover_rejects_dimensions_above_limit() -> None:
    from videoroll.apps.orchestrator_api.services.image_validation import (
        validate_and_reencode_cover,
    )

    payload = _image_bytes("PNG", size=(9000, 1))

    with pytest.raises(HTTPException) as exc:
        validate_and_reencode_cover(io.BytesIO(payload))

    assert exc.value.status_code == 413


def test_cover_rejects_decompression_bomb_pixel_budget(monkeypatch) -> None:
    from videoroll.apps.orchestrator_api.services import image_validation

    monkeypatch.setattr(image_validation, "MAX_IMAGE_PIXELS", 4)

    with pytest.raises(HTTPException) as exc:
        image_validation.validate_and_reencode_cover(
            io.BytesIO(_image_bytes("PNG", size=(3, 2)))
        )

    assert exc.value.status_code == 413


def test_cover_upload_uses_decoded_format_not_filename_or_claimed_type(monkeypatch) -> None:
    task = Mock(id=uuid.uuid4())
    db = Mock()
    db.get.return_value = task
    uploaded: dict[str, object] = {}
    s3 = Mock()

    def capture_upload(path: Path, key: str, content_type: str | None) -> None:
        uploaded.update(data=path.read_bytes(), key=key, content_type=content_type)

    s3.upload_file.side_effect = capture_upload

    async def immediate_threadpool(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asset_service, "run_in_threadpool", immediate_threadpool)
    upload_file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    upload_file.write(_image_bytes("PNG"))
    upload_file.seek(0)
    upload = UploadFile(
        upload_file,
        filename="attacker.svg",
        headers=Headers({"content-type": "image/svg+xml"}),
    )

    asset = asyncio.run(asset_service.upload_task_cover(task.id, upload, db=db, s3=s3))

    assert asset.kind == AssetKind.cover_image
    assert str(uploaded["key"]).endswith(".png")
    assert uploaded["content_type"] == "image/png"
    assert bytes(uploaded["data"]).startswith(b"\x89PNG\r\n\x1a\n")
