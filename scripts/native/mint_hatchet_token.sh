#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${VIDEOROLL_NATIVE_ENV:-/etc/videoroll/videoroll.env}"
CONFIG_DIR="${VIDEOROLL_HATCHET_CONFIG_DIR:-/opt/videoroll/current/data/hatchet/config}"

test -f "$ENV_FILE" || { echo "missing $ENV_FILE" >&2; exit 1; }
test -d "$CONFIG_DIR" || { echo "missing Hatchet config: $CONFIG_DIR" >&2; exit 1; }

exec python3 "$ROOT_DIR/scripts/native/env_exec.py" "$ENV_FILE" \
  /bin/bash -c 'export DATABASE_URL="$HATCHET_DATABASE_URL"; exec /usr/local/bin/hatchet-admin token create --config "$1" --name videoroll-native --expiresIn 87600h' \
  bash "$CONFIG_DIR"
