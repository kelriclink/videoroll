#!/usr/bin/env bash
set -euo pipefail
export LD_LIBRARY_PATH=/opt/videoroll/lib
systemctl is-active --quiet videoroll.target || exit 0
[ "$(/opt/videoroll/bin/redis-cli-real ping 2>/dev/null || true)" = PONG ] || exit 1
for svc in $(systemctl show -P Wants videoroll.target); do
  case "$svc" in
    videoroll@*.service|videoroll-social@*.service) ;;
    *) continue ;;
  esac
  if ! systemctl is-active --quiet "$svc"; then
    systemctl start --no-block "$svc" || logger -t videoroll-resume "Failed to enqueue $svc"
  fi
done
for svc in videoroll@dubbing-workflow-worker.service videoroll@render-worker.service; do
  if systemctl is-enabled --quiet "$svc" && ! systemctl is-active --quiet "$svc"; then
    systemctl start --no-block "$svc" || logger -t videoroll-resume "Failed to enqueue $svc"
  fi
done
