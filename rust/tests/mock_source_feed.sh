#!/bin/sh
# yt-dlp-compatible --flat-playlist line-delimited synthetic metadata, no network.
# Test-only invocation capture, never used in production.
if [ -n "$VIDEOROLL_V2_MOCK_YT_LOG" ]; then
  for item in "$@"; do printf '%s\n' "$item" >> "$VIDEOROLL_V2_MOCK_YT_LOG"; done
fi
for arg in "$@"; do
  if [ "$arg" = "--version" ]; then
    # Synthetic patched version used ONLY for isolated integration.
    printf '%s\n' '2026.08.01'
    exit 0
  fi
done
output=''
previous=''
for item in "$@"; do
  if [ "$previous" = "-o" ]; then output="$item"; break; fi
  previous="$item"
done
if [ -n "$output" ]; then
  dir="$(dirname "$output")"
  mkdir -p "$dir"
  printf '%s\n' '[download] vr2|2000|10000|NA|2000.0|3| 20.0%'
  sleep 3
  cp "$VIDEOROLL_V2_MOCK_YT_VIDEO" "$dir/source.mp4"
  printf '%s\n' '[download] vr2|10000|10000|NA|5000.0|0|100.0%'
  exit 0
fi
for arg in "$@"; do
  if [ "$arg" = "--dump-single-json" ]; then
    printf '%s\n' '{"id":"0123456789A","title":"English title of test","description":"A synthetic tech explainer","webpage_url":"https://youtu.be/0123456789A","uploader":"Test Channel","upload_date":"20261122","duration":23.5}'
    exit 0
  fi
done
printf '%s\n' '{"id":"0123456789E","title":"one"}' \
 '{"id":"0123456789F","title":"two"}' \
 '{"id":"0123456789E","title":"duplicate"}'
