#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-}"
if [[ -n "$ENV_FILE" ]]; then
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "env file not found: $ENV_FILE" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"
STORAGE_HOST_ROOT="${STORAGE_HOST_ROOT:-./data/storage}"

if [[ ! "$APP_UID" =~ ^[0-9]+$ || ! "$APP_GID" =~ ^[0-9]+$ ]]; then
  echo "APP_UID and APP_GID must be numeric" >&2
  exit 1
fi

directories=(
  data/ffplayout/db
  data/ffplayout/logs
  data/ffplayout/playlists
  data/ffplayout/public
  "$STORAGE_HOST_ROOT/playout-media"
)

install -d -m 0750 "${directories[@]}"

current_uid="$(id -u)"
current_gid="$(id -g)"
if [[ "$current_uid" == "0" ]]; then
  chown -R "$APP_UID:$APP_GID" data/ffplayout "$STORAGE_HOST_ROOT/playout-media"
elif [[ "$current_uid" != "$APP_UID" || "$current_gid" != "$APP_GID" ]]; then
  echo "directory ownership must be prepared by root or by APP_UID:APP_GID ($APP_UID:$APP_GID)" >&2
  exit 1
fi

echo "Prepared ffplayout directories for uid=$APP_UID gid=$APP_GID"
