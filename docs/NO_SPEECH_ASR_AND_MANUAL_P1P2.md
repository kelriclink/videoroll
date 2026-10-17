# No-speech ASR guard, manual dual-part publication and VoxCPM2 GPU protection

## OpenVINO + Silero VAD

The `openvino` native ASR installation mode installed `openvino-genai` but omitted
`faster-whisper`. `_detect_silero_speech_spans()` silently returned `None` for
that import failure, making the OpenVINO wrapper transcribe entire 120s audio
windows despite `openvino_vad_enabled=true`. This can produce hallucinated cues
on instrumentals and music.

- Pin `faster-whisper==1.2.1` (includes ONNX Runtime Silero VAD) for **both**
  native `subtitle` and `subtitle-workflow` venvs in the OpenVINO install mode.
- With VAD enabled, any missing dependency or runtime failure now raises an
  actionable ASR error rather than transcribing unfiltered audio.
- Reject OpenVINO hallucinations where one-to-three words occupy eight or more
  seconds of an ASR chunk; clip timestamp boundaries to actual audio duration.
  The conservative heuristic applies only to OpenVINO, not other engines.
- In automatic mode, zero credible ASR segments means `NO_SPEECH_DETECTED`
  with the message `未检测到可信语音`. The task is stopped for manual review; the
  parent workflow explicitly skips render, dub and publication. Ordinary
  subtitle jobs may still be run manually with alternative settings.
- Resume preserves this no-speech result; it cannot reuse an empty segments
  artifact and proceed to LLM translation or YouDub by mistake.

### Real media validation on link (2026-10-17)

- NASA `iTHUUjTA-LI`, music-heavy 83.29s track, formerly mis-transcribed as
  `The`, `Thank you.` repeated in 30s chunks: real Silero VAD now detects
  **zero speech spans**, and OpenVINO ASR returns **zero segments** without
  allocating a GPU Whisper pipeline.
- NASA `mrqXTHiphn0` 87s video with speech: VAD detects **11 speech spans**
  using the same 0.5 threshold. This is an anti-false-positive control.

## Manual P1/P2

`src/web/src/lib/publish.ts` previously looked for
`job.output.subtitle_video_key` which the current native YouDub audio-only P2
runner does not emit. Its `job.request.subtitle_video_key` is the original raw
video, NOT the P1 final asset. Select the P1 from the current task's
`video_final` asset and the P2 from a succeeded YouDub's `video_key` that also
exists among current task `video_dubbed` assets. Respect selected P1, check
current task ID (where supplied), and reject partial pairs.

No automatic resubmission of archived videos is attempted by this fix.

## VoxCPM2 scheduling

Before loading the model for missing TTS clips, check NVIDIA free GPU memory.
Default minimum is 9216 MiB (`VOXCPM_MIN_FREE_GPU_MIB` override). If the shared
V100 has insufficient free VRAM, report `VOXCPM_GPU_MEMORY_BUSY` with the actual
free and required memory. Hatchet makes at most two retries with backoff; then
records an actionable error. Completed WAV clips survive requeue/resume and
are not synthesized again. If all TTS WAVs already exist, avoid the GPU model
load and preflight entirely.

This is a headroom guard, not a reservation or absolute OOM guarantee. Qwen
running in the same GPU can still change its allocation between checking and
model inference; the limit can be tuned based on operational measurements.
No production GPU services are stopped by this change.
