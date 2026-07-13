from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError


ALLOWED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
MAX_COVER_INPUT_BYTES = 50 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 40_000_000

_FORMAT_PROPERTIES = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    content_type: str
    extension: str
    width: int
    height: int


def _read_bounded(file_obj: Any) -> bytes:
    try:
        file_obj.seek(0)
    except Exception:
        pass
    payload = file_obj.read(MAX_COVER_INPUT_BYTES + 1)
    if not isinstance(payload, (bytes, bytearray)):
        raise HTTPException(status_code=400, detail="invalid cover image stream")
    if len(payload) > MAX_COVER_INPUT_BYTES:
        raise HTTPException(status_code=413, detail="cover image upload is too large")
    if not payload:
        raise HTTPException(status_code=400, detail="cover image is empty")
    return bytes(payload)


def _canonical_mode(image: Image.Image, image_format: str) -> str:
    if image_format == "JPEG":
        return "RGB"
    return "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"


def validate_and_reencode_cover(file_obj: Any) -> ValidatedImage:
    payload = _read_bounded(file_obj)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as decoded:
                image_format = str(decoded.format or "").upper()
                if image_format not in ALLOWED_IMAGE_FORMATS:
                    raise HTTPException(
                        status_code=400,
                        detail="cover must decode as JPEG, PNG, or WebP",
                    )
                width, height = decoded.size
                if (
                    width <= 0
                    or height <= 0
                    or width > MAX_IMAGE_DIMENSION
                    or height > MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise HTTPException(
                        status_code=413,
                        detail="cover image dimensions are too large",
                    )
                decoded.seek(0)
                decoded.load()
                mode = _canonical_mode(decoded, image_format)
                pixels = decoded.convert(mode)
                canonical = Image.new(mode, decoded.size)
                canonical.paste(pixels)

        output = io.BytesIO()
        if image_format == "JPEG":
            canonical.save(output, format="JPEG", quality=90, optimize=True)
        elif image_format == "PNG":
            canonical.save(output, format="PNG", optimize=True)
        else:
            canonical.save(output, format="WEBP", lossless=True, method=6)
        content_type, extension = _FORMAT_PROPERTIES[image_format]
        return ValidatedImage(
            data=output.getvalue(),
            content_type=content_type,
            extension=extension,
            width=width,
            height=height,
        )
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413, detail="cover image dimensions are too large") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid cover image") from exc
