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
