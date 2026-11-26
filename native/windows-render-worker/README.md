# VideoRoll Native Windows Render Worker

This directory contains the native Rust implementation of the VideoRoll Render Worker.

All runtime files live inside the install directory. No VideoRoll worker state is written to a second AppData tree.

## Installed layout

```text
VideoRoll Render Worker/
├─ VideoRollRenderWorker.exe
├─ bin/
│  ├─ ffmpeg.exe
│  └─ ffprobe.exe
├─ config/
│  ├─ config.json
│  └─ credential.json
├─ logs/
│  └─ render-worker.log
├─ cache/
└─ work/
```

The installer uses a per-user writable program directory, so config, credentials, logs and work files can stay under `{app}` without requiring administrator elevation.

## Implemented worker protocol

- one-time remote enrollment (`vre_*`)
- persistent worker credential
- worker heartbeat and resource reporting
- pull claim
- execution lease heartbeat
- coordinator cancellation polling / fencing
- artifact download and SHA256 validation
- burn-in rendering through bundled FFmpeg
- soft-sub muxing
- multipart output upload
- complete / retryable-fail settlement
- local device scheduler
- Intel QSV capability probing
- NVIDIA NVENC per-GPU capability probing
- CPU fallback

QSV/NVENC support is validated with a real 64x64 single-frame encode before an encoder is advertised to the coordinator.

## Packaging

`scripts/build_native_windows_render_worker.ps1` creates:

- `dist/VideoRollRenderWorker-Native-Portable-x64.zip`
- `dist/VideoRollRenderWorker-Setup-x64.exe`

The Inno Setup installer defaults to `{userpf}\VideoRoll Render Worker` and runs with `PrivilegesRequired=lowest`.

Uninstall removes generated `config`, `logs`, `cache`, and `work` directories so the worker leaves no VideoRoll runtime state elsewhere.


## Experimental Intel QSV ASS event cache

The optional `VIDEOROLL_RENDER_QSV_EVENT_CACHE=1` environment variable enables
a sparse full-frame transparent PNG timeline for standard static ASS subtitles.
The planner is shared with the Linux Rust media module. The cache is local to
the execution work directory, uses generated relative filenames, and is
removed on completion, error or cancellation. Animated, malformed and
noncanonical ASS, short clips, high-churn timelines, and over-budget caches
retain the existing live QSV render path. Admission conservatively limits raw
RGBA states to 80% of a 256 MiB cache budget; the actual PNG total is checked.
Output dimensions, nominal FPS and duration are checked before accepting the
QSV cache output; unsuccessful renders fall back to live overlay.

The feature is **off by default** pending Windows/D3D11VA hardware validation.
The Linux-side native worker module test verified the generated cache using
FFmpeg software overlay against live libass on a 640x360 60 FPS 8 s fixture
(480/480 decoded-frame hashes identical) and verified cache cleanup. This is
not a Windows executable build or QSV driver/bit-depth/throughput acceptance.
The initial version uses full-frame overlays, so high-resolution long videos
may exceed its admission budget; band cropping remains a separate follow-up.

From the repository root, run the portable module tests:

```sh
cd rust
VIDEOROLL_TEST_QSV_CACHE_FFMPEG=/usr/bin/ffmpeg cargo test --test windows_render_cache -- --nocapture
```
