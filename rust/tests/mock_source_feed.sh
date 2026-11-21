#!/bin/sh
# yt-dlp-compatible --flat-playlist line-delimited synthetic metadata, no network.
printf '%s\n' '{"id":"0123456789E","title":"one"}' \
 '{"id":"0123456789F","title":"two"}' \
 '{"id":"0123456789E","title":"duplicate"}'
