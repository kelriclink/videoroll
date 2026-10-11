#!/usr/bin/env bash
# Real-process native smoke test. Does not create tasks or touch user media.
set -euo pipefail

scope="${1:-full}"
if [[ "$scope" != "core" && "$scope" != "full" ]]; then
  echo "usage: smoke_runtime.sh [core|full]" >&2
  exit 64
fi
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run with sudo: root is needed only to read /opt/videoroll/config/videoroll.env" >&2
  exit 1
fi
env_file="${VIDEOROLL_NATIVE_ENV:-/opt/videoroll/config/videoroll.env}"
python_bin="${VIDEOROLL_CORE_PYTHON:-/opt/videoroll/venvs/core/bin/python}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
base_url="${VIDEOROLL_SMOKE_BASE_URL:-http://127.0.0.1:3001}"
if [[ ! -f "$env_file" || ! -x "$python_bin" ]]; then
  echo "Native environment and core Python must be installed first" >&2
  exit 1
fi

echo "[1] PostgreSQL connectivity (business + Hatchet DB)"
"$python_bin" "$root/scripts/native/env_exec.py" "$env_file" \
  "$python_bin" "$root/scripts/native/preflight.py" --connect

echo "[2] Real service-user filesystem permissions"
for config in redis.conf nginx.conf; do
  runuser -u videoroll -- test -r "/opt/videoroll/config/$config" || {
    echo "Cannot read /opt/videoroll/config/$config as videoroll" >&2
    exit 1
  }
done
if runuser -u videoroll -- test -r "$env_file"; then
  echo "Sensitive environment file readable by non-root account" >&2
  exit 1
fi

echo "[3] Redis real write/read/delete with an expiring, random probe key"
/opt/videoroll/bin/redis-cli -h 127.0.0.1 -p 6379 ping | grep -qx PONG
probe="videoroll:native-smoke:$("$python_bin" -c 'import secrets; print(secrets.token_hex(12))')"
trap '/opt/videoroll/bin/redis-cli -h 127.0.0.1 -p 6379 del "$probe" >/dev/null 2>&1 || true' EXIT
/opt/videoroll/bin/redis-cli -h 127.0.0.1 -p 6379 set "$probe" native-smoke NX EX 30 | grep -qx OK
/opt/videoroll/bin/redis-cli -h 127.0.0.1 -p 6379 get "$probe" | grep -qx native-smoke
/opt/videoroll/bin/redis-cli -h 127.0.0.1 -p 6379 del "$probe" | grep -qx 1
trap - EXIT

echo "[4] Nginx and Orchestrator actual HTTP response through the gateway"
"$python_bin" - "$base_url" <<'PY'
import json
import sys
from urllib.request import urlopen
base=sys.argv[1].rstrip("/")
with urlopen(base+"/", timeout=8) as response:
    html=response.read(8192)
    if response.status != 200 or b"<html" not in html.lower():
        raise RuntimeError("VideoRoll SPA was not served")
with urlopen(base+"/api/health", timeout=8) as response:
    payload=json.load(response)
    if response.status != 200 or payload.get("status") != "ok":
        raise RuntimeError(f"Orchestrator returned unexpected health: {payload}")
print("VideoRoll Web + Nginx + Orchestrator: HTTP 200 with expected body")
PY

if [[ "$scope" == "full" ]]; then
  echo "[5] Hatchet API and all native worker/API services"
  curl --fail --silent --show-error --max-time 8 http://127.0.0.1:8888/api/ready >/dev/null
  bash "$root/scripts/native/health.sh"
fi

echo "Native $scope smoke PASSED (real PostgreSQL, Redis and HTTP; no mocks)."
