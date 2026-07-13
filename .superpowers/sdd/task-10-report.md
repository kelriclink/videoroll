# Task 10 Report: Upload And Asset Streaming Boundaries

## Result

DONE

## Root cause

- Cover validation trusted the caller-controlled MIME prefix (`image/*`) and then stored the original suffix and content type without decoding the payload.
- Asset download/stream responses trusted S3 `ContentType` and used inline disposition for every asset kind, so active content such as SVG could be rendered in the application origin.

## RED

Command:

```bash
PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py -q
```

Observed before implementation:

```text
2 failed, 4 passed in 7.72s
ModuleNotFoundError: ...services.image_validation
AssertionError: assert 'image/svg+xml' == 'application/octet-stream'
```

The first failure established that the trusted decoder boundary did not exist. The second reproduced the stored active-content type being returned inline without hardened headers.

After expanding the focused cases, the missing decoder/helper interfaces continued to fail before production implementation.

## GREEN

Initial focused green command:

```bash
PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py -q
```

Observed:

```text
16 passed in 5.18s
```

Final focused regression command:

```bash
INTERNAL_API_SECRET=task10-focused-test-secret PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py tests/test_upload_guardrails.py -q
```

Observed:

```text
22 passed, 3 warnings in 10.37s
```

Compile and whitespace verification:

```bash
PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m compileall -q src/videoroll/apps/orchestrator_api/services/image_validation.py src/videoroll/apps/orchestrator_api/services/asset_service.py src/videoroll/apps/orchestrator_api/routers/assets.py tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py tests/test_upload_guardrails.py
git diff --check -- pyproject.toml src/videoroll/apps/orchestrator_api/services/image_validation.py src/videoroll/apps/orchestrator_api/services/asset_service.py src/videoroll/apps/orchestrator_api/routers/assets.py tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py tests/test_upload_guardrails.py
```

Observed: exit code 0 with no output.

## Changes

- Added Pillow with a Python 3.12-compatible upper bound.
- Added a bounded trusted decoder accepting only JPEG, PNG, and WebP.
- Enforced encoded-byte, dimension, and decoded-pixel limits; Pillow decompression-bomb warnings fail closed.
- Re-encoded decoded pixels into canonical formats without EXIF, PNG text, ICC, or other caller metadata.
- Stored the canonical extension and content type rather than filename/MIME claims.
- Added per-asset-kind response type allow-lists, `X-Content-Type-Options: nosniff`, and attachment disposition for images/text/unknown content.
- Retained inline playback only for explicitly safe video/audio kinds carrying safe media types.
- Hardened full, range, invalid-range, and download response construction through the same header/type helpers.

## Self-review

- No caller-supplied MIME type or suffix participates in cover storage after validation.
- SVG/polyglot trailing data cannot survive raster re-encoding.
- The decoder reads at most 50 MiB and rejects images above 8192 pixels per axis or 40 million decoded pixels before `load()`.
- Response media types are normalized against asset-kind allow-lists; active or mismatched stored types become `application/octet-stream`.
- All non-video/non-audio streams are attachments, including valid raster cover images.
- Temporary canonical uploads use an in-memory spooled file and are closed by the existing upload cleanup path.
- Only Task 10 allow-listed source/test files were changed; concurrent Task 2/9 files were not touched.

## Concerns

- The exact three-file pytest command initially failed during collection because concurrent Task 2 now rejects the default `INTERNAL_API_SECRET` outside explicit development mode. The same suite passed with a non-default test-only secret; no Task 2 file was modified.
- The shared virtual environment runs Python 3.14.6, while the project contract is Python 3.12+. Pillow 12.3.0 installed and passed the focused codec tests; the declared dependency range (`>=11.3,<13`) supports Python 3.12.

## Follow-up review: memory budget and response coverage

### Root cause

- The 40MP decoded-pixel allowance allowed a 120--160 MiB RGB/RGBA canvas. The old canonicalization then created a converted canvas, an `Image.new` canvas, and copied pixels into it, so one request could retain several complete decoded-image buffers.
- The response tests covered only the non-range stream result. They did not construct actual download, 206, and 416 responses to assert the final hardened headers.

### RED

Command:

```bash
INTERNAL_API_SECRET=task10-focused-test-secret PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest tests/test_upload_content_validation.py::test_cover_rejects_image_just_above_the_safe_16_megapixel_budget -q
```

Observed before implementation:

```text
FAILED: DID NOT RAISE HTTPException
1 failed in 10.53s
```

The 4096 x 3907 (16,003,072-pixel) PNG was accepted under the former 40MP policy.

### GREEN

Command:

```bash
INTERNAL_API_SECRET=task10-focused-test-secret PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest tests/test_upload_content_validation.py::test_cover_rejects_image_just_above_the_safe_16_megapixel_budget tests/test_upload_content_validation.py::test_webp_cover_reencoding_strips_exif_and_xmp_metadata tests/test_upload_content_validation.py::test_cover_rejects_raster_formats_outside_the_allow_list tests/test_upload_content_validation.py::test_cover_rejects_corrupt_image_payload tests/test_orchestrator_asset_service.py::test_actual_asset_responses_always_harden_unsafe_cover_headers -q
```

Observed after implementation:

```text
9 passed, 5 warnings in 6.19s
```

### Changes and self-review

- Lowered the default decoded-pixel budget to 16MP. The independent 8192px edge limit remains; a narrow 8192px-wide cover is still accepted when it fits the 16MP total budget.
- After the trusted decoder has loaded the current frame, its encoded source buffer is closed. Images already in canonical RGB/RGBA mode are encoded directly; conversion occurs only when the mode needs normalization. This removes the prior unconditional `Image.new` + `paste` full-image copy.
- Canonical upload bytes now go through a disk-backed temporary file instead of an in-memory spooled file, avoiding a second full compressed image buffer during asset storage.
- Added regression coverage for the 16MP boundary, WebP EXIF/XMP normalization, corrupt payloads, BMP/GIF/TIFF rejection, and actual download/206/416 responses with `nosniff` plus attachment disposition.
- `git diff --check` completed without output. Per the coordinator's instruction, the complete Task 10 suite and extended validation are deferred to the unified stage rather than being re-run here.
