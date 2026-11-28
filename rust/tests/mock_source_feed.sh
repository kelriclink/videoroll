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
  case "$*" in
    *0123456789A*) printf '%s\n' '{"id":"0123456789A","title":"English title of test","description":"A synthetic tech explainer","webpage_url":"https://youtu.be/0123456789A","uploader":"Test Channel","upload_date":"20261122","duration":23.5}' > "$dir/source.info.json" ;;
  esac
  printf '%s\n' '[download] vr2|10000|10000|NA|5000.0|0|100.0%'
  exit 0
fi
for arg in "$@"; do
  if [ "$arg" = "--dump-single-json" ]; then
    printf '%s\n' '{"id":"0123456789A","title":"English title of test","description":"A synthetic tech explainer","webpage_url":"https://youtu.be/0123456789A","uploader":"Test Channel","upload_date":"20261122","duration":23.5}'
    exit 0
  fi
done
# Regression fixture: the first two entries have already been ingested,
# while a third later playlist entry is new. A limit=1 scan must reach it.
case "$*" in
  *UC77777777777777777777/shorts*)
    printf '%s\n' '{"id":"SCANFAIL003","upload_date":"20261121"}'
    exit 0
    ;;
  *UC77777777777777777777/videos*)
    printf '%s\n' '{"id":"SCANFAIL001","upload_date":"20261124"}' '{"id":"SCANFAIL002","upload_date":"20261123"}'
    exit 0
    ;;
  *UC88888888888888888888/shorts*)
    printf '%s\n' '{"id":"SCANSHORT01","upload_date":"20261125"}' '{"id":"SCANVIDEO01","upload_date":"20261123"}'
    exit 0
    ;;
  *UC88888888888888888888/videos*)
    printf '%s\n' '{"id":"SCANVIDEO01","upload_date":"20261123"}' '{"id":"SCANVIDEO02"}'
    exit 0
    ;;
  *UC99999999999999999999*)
    printf '%s\n' '{"id":"0123456789E","title":"already ingested A"}' \
      '{"id":"0123456789F","title":"already ingested B"}' \
      '{"id":"V2DEEP00001","title":"late new entry","upload_date":"20261122"}'
    exit 0
    ;;
esac
printf '%s\n' '{"id":"0123456789E","title":"one"}' \
 '{"id":"0123456789F","title":"two"}' \
 '{"id":"0123456789E","title":"duplicate"}'
