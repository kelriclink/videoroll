#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=/opt/videoroll/current
ENV_FILE="${VIDEOROLL_NATIVE_ENV:-/opt/videoroll/config/videoroll.env}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Hatchet token initialization requires root to update the protected environment file" >&2
  exit 1
fi
test -r "$ENV_FILE"
for attempt in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:8888/api/ready >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" == 60 ]]; then
    echo "Hatchet did not become ready for token initialization" >&2
    exit 1
  fi
  sleep 2
done

token="$(bash "$ROOT_DIR/scripts/native/mint_hatchet_token.sh" | tr -d '\r\n')"
if [[ ${#token} -lt 30 ]]; then
  echo "Hatchet returned no usable worker token" >&2
  exit 1
fi
python3 "$ROOT_DIR/scripts/native/set_env_value.py" "$ENV_FILE" HATCHET_CLIENT_TOKEN "$token"
unset token
echo "Hatchet worker token initialized."
