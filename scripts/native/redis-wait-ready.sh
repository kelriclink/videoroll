#!/usr/bin/env bash
set -euo pipefail
export LD_LIBRARY_PATH=/opt/videoroll/lib
for i in $(seq 1 240); do
  if [ "$(/opt/videoroll/bin/redis-cli-real ping 2>/dev/null || true)" = PONG ]; then
    # Enqueue reconciliation after redis startup completion, including on auto-restarts.
    # --no-block avoids a dependency deadlock (the helper runs After=Redis).
    systemctl start --no-block videoroll-resume-after-redis.service || true
    exit 0
  fi
  sleep 0.25
done
echo "VideoRoll Redis did not become ready within 60 seconds" >&2
exit 1
