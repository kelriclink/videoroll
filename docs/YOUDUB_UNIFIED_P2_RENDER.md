# VideoRoll YouDub audio-only → Unified GPU P2 render

## New job protocol

VideoRoll retains the original `video_raw` source. For **new** native YouDub
jobs, `VIDEOROLL_YOUDUB_AUDIO_ONLY=1` (the default) writes an `audio_only.flag`
inside the newly created YouDub upload snapshot. This mode does **not** create
or reencode `video_source.mp4`: the input video in `_uploads` is used directly
by Demucs. Legacy YouDub jobs without this flag keep their historical stages.

The existing YouDub nine-stage pipeline retains its resumable stage database.
Stages `asr`, `asr_fix`, `translate` reuse VideoRoll's Chinese subtitles. The
final `merge_video` stage in audio-only mode instead creates:

- `tmp/audio_dubbing.wav`: aligned Chinese vocal audio
- `tmp/audio_mixed.m4a`: Chinese vocal + Demucs background mix
- `metadata/subtitles.zh.srt`: subtitles regenerated from VoxCPM timing data

There is no YouDub video encode. The output path stored in YouDub's native
`final_video_path` is an audio media file **only for these audio-only jobs**.

VideoRoll persists the original video, timed SRT and mixed audio keys, and a
P2-specific `RenderJob` (`operation_key=youdub:p2-render:<dubbing-job-id>`).
Render Coordinator creates execution-scoped ASS, and Render Workers burn subtitles
onto the **original** video using normal GPU dispatch, but replace its audio
with YouDub's mixed AAC track. Output uploads must be `video_dubbed`, not
`video_final`. VideoRoll does **not** mark the parent Task rendered again.

Only successful P2 render completion changes the DubbingJob from `rendering`
to `succeeded`, persists `video_key`, and atomically emits
`workflow.dubbing.finished`, enabling the existing P1/P2 publish step.
Terminal failures and cancellation notify the waiting workflow; retry can reuse
finished VoxCPM audio without re-synthesizing all speech.

## Safety and deployment

- To opt new jobs back into legacy native video encoding, set
  `VIDEOROLL_YOUDUB_AUDIO_ONLY=0` **before** they are created. This does not
  change already created jobs.
- The bundled upstream code in `vendor/youdub_webui` must be installed into
  `/DATA/youdub-runtime` with the versioned `youdub_bridge.py --prepare` command.
  This command correctly refuses to replace runtime files if YouDub has an
  unfinished task. Never bypass this guard or modify a running native task.
- Native application releases are separately gated by `videorollctl update
  --apply`, which refuses to switch with active subtitle/render/workflow tasks.
  Stop scheduling new jobs, drain the queues, prepare YouDub, then deploy.
- Do not activate new application code with old YouDub runtime code; the two
  halves of the handoff must be aligned.

## Verification

- Vendor tests for `local_video`, `pipeline`, `ffmpeg`.
- Backend tests for P2 render artifact roles, fences, terminal events and
  preservation of P1 assets; then full suite.
- Test FFmpeg with an audio track of a different frequency from the source
  and confirm the output maps audio input 2 (GPU overlay) or 1 (fallback).
- End-to-end test native Demucs/VoxCPM2 → audio handoff → RenderJob →
  `video_dubbed` → P1/P2 publishing on a disposable sample task, with no live
  uploads to Bilibili in the initial smoke test.
