#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${VIDEOROLL_DOTENV:-/etc/videoroll/videoroll.env}"
if [[ ! -f "$ENV_FILE" && -f "$ROOT_DIR/.env" ]]; then
  ENV_FILE="$ROOT_DIR/.env"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "dotenv file not found: $ENV_FILE" >&2
  exit 1
fi

export VIDEOROLL_PROJECT_ROOT="$ROOT_DIR"
export VIDEOROLL_STORAGE_HOST_ROOT="${VIDEOROLL_STORAGE_HOST_ROOT:-$ROOT_DIR/data/storage}"
export VIDEOROLL_DOTENV="$(realpath "$ENV_FILE")"

redis=(docker compose -p videoroll-native-redis --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" -f "$ROOT_DIR/deploy/systemd/redis.compose.yml")
hatchet=(docker compose -p videoroll-native-hatchet --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" -f "$ROOT_DIR/deploy/systemd/hatchet.compose.yml")
social=(docker compose -p videoroll-native-social --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" -f "$ROOT_DIR/deploy/systemd/social.compose.yml")

action="${1:-}"
shift || true

case "$action" in
  config)
    "${redis[@]}" config -q
    "${hatchet[@]}" config -q
    "${social[@]}" config -q
    ;;
  build)
    "${redis[@]}" pull
    "${hatchet[@]}" build "$@"
    "${social[@]}" build "$@"
    ;;
  up-infra)
    "${redis[@]}" up -d "$@"
    "${hatchet[@]}" up -d "$@"
    ;;
  up-social)
    "${social[@]}" up -d "$@"
    ;;
  up)
    "${redis[@]}" up -d "$@"
    "${hatchet[@]}" up -d "$@"
    "${social[@]}" up -d "$@"
    ;;
  hatchet-token)
    "${hatchet[@]}" exec -T hatchet-lite sh -c '
      if [ -s /config/authdisabled-token ]; then
        cat /config/authdisabled-token
      else
        /hatchet-admin token create --config /config --name videoroll-native
      fi
    '
    ;;
  down)
    "${social[@]}" down "$@"
    "${hatchet[@]}" down "$@"
    "${redis[@]}" down "$@"
    ;;
  ps)
    echo "== Redis =="
    "${redis[@]}" ps
    echo "== Hatchet =="
    "${hatchet[@]}" ps
    echo "== Social publisher =="
    "${social[@]}" ps
    ;;
  logs)
    echo "Use one of:"
    printf '  docker compose -p videoroll-native-redis --project-directory %q --env-file %q -f %q logs -f\n' "$ROOT_DIR" "$ENV_FILE" "$ROOT_DIR/deploy/systemd/redis.compose.yml"
    printf '  docker compose -p videoroll-native-hatchet --project-directory %q --env-file %q -f %q logs -f\n' "$ROOT_DIR" "$ENV_FILE" "$ROOT_DIR/deploy/systemd/hatchet.compose.yml"
    printf '  docker compose -p videoroll-native-social --project-directory %q --env-file %q -f %q logs -f\n' "$ROOT_DIR" "$ENV_FILE" "$ROOT_DIR/deploy/systemd/social.compose.yml"
    ;;
  *)
    echo "usage: $0 {config|build|up-infra|up-social|up|hatchet-token|down|ps|logs} [docker-compose args...]" >&2
    exit 64
    ;;
esac
