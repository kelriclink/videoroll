# Event-timestamped ASS overlay on Intel VAAPI

## Motivation

Prior VideoRoll VAAPI static ASS pipeline rasterized only unique ASS states,
but sent those same PNG images over `image2pipe` **15 times/second**, even when
the overlay did not change. This wasted PNG decoding, host-to-device transfers
and added up to 66.7ms of overlay timing quantization on a 60fps video.

## Implementation

1. Keep libass and the existing cropped subtitle-band / full-frame selection.
   Cache one transparent PNG for every static ASS event boundary (including
   subtitle removal). The cache renderer already supports millisecond PTS via
   FFmpeg's `ffconcat` demuxer and `option framerate 1000`.
2. Emit a validated *generated-only* `overlay_events.ffconcat` with one `file`
   per cached PNG and `duration` entries up to the next subtitle boundary.
   The image input's time base is 1/1000s; no arbitrary user paths or
   directives are accepted. **Do not append a duplicate final PNG**: adding
   a frame at source EOF can extend `overlay_vaapi` output by one frame.
3. Use the timed PNG stream as the secondary input of `overlay_vaapi` with
   `shortest=0:repeatlast=1:eof_action=repeat`. Framesync reuses the latest
   texture until the next event PTS. The primary video stays fully on the
   VAAPI hardware decode/composition/encode path where supported.
4. Preserve the exact main-video FPS, independent of subtitle event density;
   an audio replacement for P2 is passed as the third input and stream-copied.
5. ASS with motion, fades, karaoke, or complex positioning continues through
   existing live full-FPS VAAPI overlays; failures fall back to live overlay
   and then the preexisting legacy path. No unexpected software transcoding
   is triggered in the normal static subtitle path.
6. Clip ASS event boundaries exactly at the end of the input video to avoid
   creating a terminal overlay image at source EOF.

## Validation on link (2026-10-17)

- Synthetic 6s black test video with 30fps and 60fps, overlapping and gapped
  ASS events, dubbed AAC: 180/360 output frames; frame-accurate at event
  boundaries (e.g. 0.370s event visible at 0.3833s on 60fps; 0.830s end
  invisible at 0.8333s). Audio stream retained.
- Old 15fps stream required 91 PNG inputs for 6s; event-VFR used 7.
- Production sample video from task `5326802d-0f30-4c08-88b6-a9aa7bf173cd`
  (AV1 1920×1080 30fps, 69.348s, P1 ASS, actual mixed P2 audio):
  old 15fps path 1,042 PNG packets, 8.86s wall time;
  event-VFR 5 PNG packets, 7.61s wall time. Both output 2,080 30fps
  frames and AAC. VAAPI processing ~334fps vs ~407fps respectively.
- FFmpeg `ffprobe` verified manifest image PTS 0.000, 0.370, 0.830,
  1.730, 2.000 seconds in a small fixture.
- `tests/test_processing_render.py` covers manifest PTS generation,
  path safety, source EOF, live/legacy fallback and static/dynamic cases.

## Limitations

- Event-VFR optimization applies to static ASS when `overlay_vaapi` is
  available and a cache is worth building. It does **not** change NVIDIA
  `cpu-ass-nvenc` or complex ASS rendering; those need separate work.
- The secondary PNG inputs are sparse; VAAPI **still composes every video
  frame** to produce hard subtitles. The optimization removes redundant
  subtitle raster/texture updates, not necessary final video encoding.
- End-to-end speed depends on video decode, encoder and subtitle density;
  measurements above are samples rather than guaranteed improvements.

## Upstream implementation references

- FFmpeg concat demuxer: https://github.com/FFmpeg/FFmpeg/blob/master/doc/demuxers.texi
- FFmpeg overlay_vaapi framesync repeatlast/eof_action:
  https://www.ffmpeg.org/doxygen/trunk/vf__overlay__vaapi_8c.html
- libass `ass_render_frame(now, detect_change)`: https://github.com/libass/libass/blob/master/libass/ass.h
