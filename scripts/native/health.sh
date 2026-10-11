#!/usr/bin/env bash
set -euo pipefail

failed=0

check_http() {
  local name="$1"
  local url="$2"
  if curl -fsS --max-time 5 "$url" >/dev/null; then
    printf '%-28s OK   %s\n' "$name" "$url"
  else
    printf '%-28s FAIL %s\n' "$name" "$url" >&2
    failed=1
  fi
}

check_tcp() {
  local name="$1" host="$2" port="$3"
  if timeout 3 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
    printf '%-28s OK   %s:%s\n' "$name" "$host" "$port"
  else
    printf '%-28s FAIL %s:%s\n' "$name" "$host" "$port" >&2
    failed=1
  fi
}

check_http orchestrator http://127.0.0.1:8000/health
check_http subtitle-service http://127.0.0.1:8001/health
check_http youtube-ingest http://127.0.0.1:8002/health
check_http bilibili-publisher http://127.0.0.1:8003/health
check_http egress-gateway http://127.0.0.1:8020/health
check_http workflow-api http://127.0.0.1:8030/health
check_http social-publisher http://127.0.0.1:8010/health
check_http web-gateway http://127.0.0.1:3001/
check_tcp redis 127.0.0.1 6379
check_http hatchet-lite http://127.0.0.1:8888/api/ready

# HTTP alone cannot detect dead workers. A target may remain active when a
# Wants= child has crashed or was skipped by ConditionPathExists.
if ! systemctl is-active --quiet videoroll.service; then
  for role in workflow-worker subtitle-workflow-worker subtitle-control-worker publish-worker outbox-dispatcher; do
    if ! systemctl is-active --quiet "videoroll@$role.service"; then
      echo "Worker $role is not active" >&2
      failed=1
    fi
  done
  # Distributed rendering is the default; an explicitly enabled local worker
  # must still be healthy and must not be silently started by upgrades.
  if systemctl is-enabled --quiet videoroll@render-worker.service \
    || systemctl is-active --quiet videoroll@render-worker.service; then
    if ! systemctl is-active --quiet videoroll@render-worker.service; then
      echo "Enabled local render worker is not active" >&2
      failed=1
    fi
  fi
  for role in api worker scheduler; do
    if ! systemctl is-active --quiet "videoroll-social@$role.service"; then
      echo "Social $role is not active" >&2
      failed=1
    fi
  done
fi

# In single-service mode ffplayout is a supervised child, not its own unit.
# An explicit opt-out is supported for installations without the binary.
if systemctl is-active --quiet videoroll.service; then
  ffplayout_skip="${VIDEOROLL_SKIP_FFPLAYOUT:-}"
  if [[ -z "$ffplayout_skip" && -r /etc/videoroll/videoroll.env ]]; then
    ffplayout_skip="$(sed -n 's/^VIDEOROLL_SKIP_FFPLAYOUT=//p' /etc/videoroll/videoroll.env | tail -n 1 | tr -d '\"')"
  fi
  if [[ "$ffplayout_skip" != "1" && "$ffplayout_skip" != "true" && -x /usr/local/bin/ffplayout ]]; then
    check_http ffplayout http://127.0.0.1:8787/api/setup
  fi
elif systemctl is-active --quiet videoroll-ffplayout.service; then
  check_http ffplayout http://127.0.0.1:8787/api/setup
fi

exit "$failed"
