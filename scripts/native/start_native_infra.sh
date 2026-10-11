#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${VIDEOROLL_NATIVE_ENV:-/etc/videoroll/videoroll.env}"
test -s "$ENV_FILE"

systemctl start videoroll-redis.service
systemctl start videoroll-hatchet.service

ready=0
for attempt in $(seq 1 60); do
  if timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/6379' 2>/dev/null \
    && curl -fsS --max-time 3 http://127.0.0.1:8888/api/ready >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != "1" ]]; then
  echo "Native Redis/Hatchet did not become ready" >&2
  systemctl status videoroll-redis.service videoroll-hatchet.service --no-pager >&2 || true
  exit 1
fi

hatchet_token="$(bash "$ROOT_DIR/scripts/native/mint_hatchet_token.sh" | tr -d '\r\n')"
if [[ -z "$hatchet_token" ]]; then
  echo "Hatchet returned an empty worker token" >&2
  exit 1
fi
python3 "$ROOT_DIR/scripts/native/set_env_value.py" "$ENV_FILE" HATCHET_CLIENT_TOKEN "$hatchet_token"
unset hatchet_token
echo "Native Redis/Hatchet ready and token persisted."
