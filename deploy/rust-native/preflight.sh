#!/usr/bin/env bash
set -euo pipefail
# Refuse coexistence with the known old application roles; never stop them
# automatically. A manually launched broker consumer still requires operator
# confirmation. ffplayout is deliberately not a conflict.
for unit in videoroll.target videoroll@orchestrator.service videoroll@outbox-dispatcher.service videoroll@workflow-api.service videoroll@workflow-worker.service videoroll@subtitle-workflow-worker.service videoroll@subtitle-control-worker.service videoroll@publish-worker.service videoroll@render-worker.service videoroll@youtube-ingest.service videoroll@subtitle-service.service videoroll@bilibili-publisher.service videoroll@egress-gateway.service videoroll-social@api.service videoroll-social@worker.service videoroll-social@scheduler.service videoroll-web.service; do
  state=$(systemctl show "$unit" -p ActiveState --value)
  case "$state" in active|activating|reloading|deactivating) echo "old application role still active: $unit" >&2; exit 1;; esac
done
: "${VIDEOROLL_V2_STORAGE_ROOT:?explicit storage root required}"
test -d "$VIDEOROLL_V2_STORAGE_ROOT" && test -r "$VIDEOROLL_V2_STORAGE_ROOT" && test -w "$VIDEOROLL_V2_STORAGE_ROOT"
for executable in "${VIDEOROLL_V2_FFMPEG:-ffmpeg}" "${VIDEOROLL_V2_FFPROBE:-ffprobe}" "${VIDEOROLL_V2_YT_DLP:-yt-dlp}"; do
  command -v "$executable" >/dev/null
done
exec /opt/videoroll-rust/current/bin/videoroll-v2 standalone-preflight --startup
