#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
BASE_COMPOSE_FILE="${BASE_COMPOSE_FILE:-docker-compose.yml}"
INTEL_COMPOSE_FILE="${INTEL_COMPOSE_FILE:-docker-compose.intel.yml}"
INTEL_MODE="${VIDEOROLL_INTEL_GPU_COMPOSE:-auto}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi
if [[ ! -f "$BASE_COMPOSE_FILE" ]]; then
  echo "base compose file not found: $BASE_COMPOSE_FILE" >&2
  exit 1
fi

env_value() {
  local key="$1"
  local value
  value="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r')"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

intel_enabled=0
case "${INTEL_MODE,,}" in
  1|true|yes|on)
    intel_enabled=1
    ;;
  0|false|no|off)
    intel_enabled=0
    ;;
  auto)
    render_device="${INTEL_GPU_RENDER_DEVICE:-$(env_value INTEL_GPU_RENDER_DEVICE)}"
    render_device="${render_device:-/dev/dri/renderD128}"
    configured_gid="${INTEL_GPU_RENDER_GID:-$(env_value INTEL_GPU_RENDER_GID)}"
    asr_engine="${SUBTITLE_ASR_ENGINE:-$(env_value SUBTITLE_ASR_ENGINE)}"
    if [[ -f "$INTEL_COMPOSE_FILE" && -e "$render_device" ]] \
      && [[ "${asr_engine,,}" == "openvino" || -n "$configured_gid" ]]; then
      intel_enabled=1
    fi
    ;;
  *)
    echo "invalid VIDEOROLL_INTEL_GPU_COMPOSE=$INTEL_MODE (expected auto/true/false)" >&2
    exit 1
    ;;
esac

compose_args=(docker compose --env-file "$ENV_FILE" -f "$BASE_COMPOSE_FILE")

if [[ "$intel_enabled" == "1" ]]; then
  if [[ ! -f "$INTEL_COMPOSE_FILE" ]]; then
    echo "Intel GPU compose override not found: $INTEL_COMPOSE_FILE" >&2
    exit 1
  fi
  render_device="${INTEL_GPU_RENDER_DEVICE:-$(env_value INTEL_GPU_RENDER_DEVICE)}"
  render_device="${render_device:-/dev/dri/renderD128}"
  if [[ ! -e "$render_device" ]]; then
    echo "Intel GPU render device not found: $render_device" >&2
    exit 1
  fi
  host_gid="$(stat -c '%g' "$render_device")"
  configured_gid="${INTEL_GPU_RENDER_GID:-$(env_value INTEL_GPU_RENDER_GID)}"
  if [[ -n "$configured_gid" && "$configured_gid" != "$host_gid" ]]; then
    echo "INTEL_GPU_RENDER_GID=$configured_gid does not match $render_device group id $host_gid" >&2
    exit 1
  fi
  export INTEL_GPU_RENDER_GID="${configured_gid:-$host_gid}"
  compose_args+=(-f "$INTEL_COMPOSE_FILE")
  echo "Using Intel GPU compose override: $INTEL_COMPOSE_FILE (render GID $INTEL_GPU_RENDER_GID)" >&2
else
  echo "Using base Compose without Intel GPU override" >&2
fi

exec "${compose_args[@]}" "$@"
