#!/usr/bin/env bash
set -euo pipefail
# Development-only integration smoke test. PostgreSQL runs in an ephemeral
# Docker container; Hatchet itself runs as three native Linux binaries.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARTIFACT_DIR="${HATCHET_ARTIFACT_DIR:-/tmp/videoroll-native-hatchet-artifacts}"
BIN="$ARTIFACT_DIR/bin"
PG_PORT="${HATCHET_SMOKE_PG_PORT:-15432}"
HTTP_PORT="${HATCHET_SMOKE_HTTP_PORT:-18888}"
GRPC_PORT="${HATCHET_SMOKE_GRPC_PORT:-17077}"
FRONTEND_PORT="${HATCHET_SMOKE_FRONTEND_PORT:-18081}"
NAME=videoroll-hatchet-native-smoke
WORK="$(mktemp -d)"
HATCHET_PID=""

cleanup() {
  if [[ -n "$HATCHET_PID" ]]; then
    kill "$HATCHET_PID" 2>/dev/null || true
    wait "$HATCHET_PID" 2>/dev/null || true
  fi
  docker stop "$NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
for bin in hatchet-lite hatchet-admin hatchet-migrate; do
  [[ -x "$BIN/$bin" ]] || { echo "missing native Hatchet binary: $bin" >&2; exit 1; }
done
[[ -s "$ARTIFACT_DIR/static-assets/index.html" ]]
command -v docker >/dev/null || { echo "Docker is required only for the temporary test database" >&2; exit 1; }
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "test container name already in use; aborting" >&2
  exit 1
fi

docker run -d --rm --name "$NAME" \
  -e POSTGRES_USER=hatchet -e POSTGRES_PASSWORD=Aa1smoke_test_password \
  -e POSTGRES_DB=hatchet \
  -p "127.0.0.1:$PG_PORT:5432" postgres:17-alpine >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U hatchet -d hatchet >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo "test PostgreSQL not ready" >&2; exit 1; }

export DATABASE_URL="postgresql://hatchet:Aa1smoke_test_password@127.0.0.1:$PG_PORT/hatchet"
export ADMIN_EMAIL=admin@videoroll.local ADMIN_PASSWORD=Aa1smoke_test_password ADMIN_NAME=VideoRoll
export DEFAULT_TENANT_NAME=VideoRoll DEFAULT_TENANT_SLUG=videoroll
export LITE_STATIC_ASSET_DIR="$ARTIFACT_DIR/static-assets"
export LITE_FRONTEND_BASE_PATH=/workflow-ui
export LITE_FRONTEND_PORT="$FRONTEND_PORT" LITE_RUNTIME_PORT="$HTTP_PORT"
export SERVER_MSGQUEUE_KIND=postgres SERVER_DEFAULT_ENGINE_VERSION=V1
export SERVER_GRPC_BIND_ADDRESS=127.0.0.1
export SERVER_GRPC_BROADCAST_ADDRESS="127.0.0.1:$GRPC_PORT"
export SERVER_INTERNAL_CLIENT_INTERNAL_GRPC_BROADCAST_ADDRESS="127.0.0.1:$GRPC_PORT"
export SERVER_GRPC_PORT="$GRPC_PORT" SERVER_GRPC_INSECURE=true
export SERVER_URL=http://127.0.0.1:13001
export SERVER_FRONTEND_URL=http://127.0.0.1:13001/workflow-ui/
export SERVER_AUTH_COOKIE_DOMAIN=localhost SERVER_AUTH_COOKIE_INSECURE=true
export SERVER_AUTH_SET_EMAIL_VERIFIED=true SERVER_SECURITY_CHECK_ENABLED=false SERVER_LOGGER_LEVEL=warn

cd "$WORK"
"$BIN/hatchet-migrate" > migrate.log 2>&1 || {
  tail -50 migrate.log >&2
  exit 1
}
"$BIN/hatchet-admin" quickstart --skip certs --generated-config-dir "$WORK/config" --overwrite=false > quickstart.log 2>&1 || {
  tail -50 quickstart.log >&2
  exit 1
}
"$BIN/hatchet-lite" --config "$WORK/config" > lite.log 2>&1 &
HATCHET_PID="$!"

ready=0
for _ in $(seq 1 75); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/ready" >/dev/null 2>&1; then
    ready=1
    break
  fi
  kill -0 "$HATCHET_PID" 2>/dev/null || { tail -65 lite.log >&2; exit 1; }
  sleep 1
done
[[ "$ready" == 1 ]] || { tail -65 lite.log >&2; exit 1; }

code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/workflow-ui/")"
[[ "$code" == 200 ]] || { echo "Hatchet frontend HTTP $code, expected 200" >&2; exit 1; }

token="$("$BIN/hatchet-admin" token create --config "$WORK/config" --name videoroll-smoke --expiresIn 24h | tail -1)"
[[ "${#token}" -gt 30 ]] || { echo "Hatchet token generation failed" >&2; exit 1; }
unset token
echo "HATCHET_NATIVE_SMOKE_PASS migrations quickstart readiness frontend token"
