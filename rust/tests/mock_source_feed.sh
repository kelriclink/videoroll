#!/bin/sh
# yt-dlp-compatible --flat-playlist line-delimited synthetic metadata, no network.
for arg in "$@"; do
  if [ "$arg" = "--dump-single-json" ]; then
    printf '%s\n' '{"id":"0123456789A","title":"English title of test","description":"A synthetic tech explainer","webpage_url":"https://youtu.be/0123456789A","uploader":"Test Channel","upload_date":"20261122","duration":23.5}'
    exit 0
  fi
done
printf '%s\n' '{"id":"0123456789E","title":"one"}' \
 '{"id":"0123456789F","title":"two"}' \
 '{"id":"0123456789E","title":"duplicate"}'
