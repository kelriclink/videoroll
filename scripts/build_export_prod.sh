#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f social-auto-upload/sau_cli.py ]]; then
  echo "social-auto-upload submodule is missing; run: git submodule update --init --recursive" >&2
  exit 1
fi

ENV_FILE="${ENV_FILE:-deploy_compose/.env}"
APP_IMAGE="${APP_IMAGE:-videoroll:prod}"
WEB_IMAGE="${WEB_IMAGE:-videoroll-web:prod}"
SOCIAL_IMAGE="${SOCIAL_IMAGE:-videoroll-social-publisher:prod}"
INCLUDE_BASE_IMAGES="${INCLUDE_BASE_IMAGES:-1}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT_TAR="${OUTPUT_TAR:-$ROOT_DIR/videoroll-prod-bundle-${TIMESTAMP}.tar}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi

set -a
. "$ENV_FILE"
set +a

DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  echo "docker daemon not reachable via '$DOCKER_BIN'. Set DOCKER_BIN or DOCKER_HOST env to override." >&2
  exit 1
fi

docker_run() {
  # shellcheck disable=SC2086
  $DOCKER_BIN "$@"
}

echo "Using env file: $ENV_FILE"
echo "Building app image: $APP_IMAGE"
docker_run build \
  -t "$APP_IMAGE" \
  --build-arg INSTALL_ASR="${INSTALL_ASR:-1}" \
  --build-arg YTDLP_VERSION="${YTDLP_VERSION:-latest}" \
  -f Dockerfile \
  .

echo "Building web image: $WEB_IMAGE"
docker_run build \
  -t "$WEB_IMAGE" \
  --build-arg VITE_ORCHESTRATOR_URL="${VITE_ORCHESTRATOR_URL:-}" \
  -f src/web/Dockerfile \
  src/web

echo "Building social publisher image: $SOCIAL_IMAGE"
docker_run build \
  -t "$SOCIAL_IMAGE" \
  -f docker/social-publisher.Dockerfile \
  .

IMAGES=("$APP_IMAGE" "$WEB_IMAGE" "$SOCIAL_IMAGE")
if [[ "$INCLUDE_BASE_IMAGES" == "1" ]]; then
  echo "Pulling base service images"
  docker_run pull redis:7
  docker_run pull minio/minio:latest
  docker_run pull minio/mc:latest
  IMAGES+=("redis:7" "minio/minio:latest" "minio/mc:latest")
fi

echo "Exporting images to: $OUTPUT_TAR"
docker_run save -o "$OUTPUT_TAR" "${IMAGES[@]}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$OUTPUT_TAR" | tee "${OUTPUT_TAR}.sha256"
fi

echo
echo "Done."
echo "Bundle: $OUTPUT_TAR"
if [[ -f "${OUTPUT_TAR}.sha256" ]]; then
  echo "SHA256: ${OUTPUT_TAR}.sha256"
fi
echo
echo "Import on target machine:"
echo "  docker load -i $(basename "$OUTPUT_TAR")"
