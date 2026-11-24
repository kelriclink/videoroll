#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
port="${VIDEOROLL_BILI_TEST_PG_PORT:-35442}"
api_port="${VIDEOROLL_BILI_TEST_API_PORT:-18180}"
bili_port="${VIDEOROLL_BILI_TEST_UPSTREAM_PORT:-18196}"
translate_port="${VIDEOROLL_BILI_TEST_TRANSLATE_PORT:-18198}"
for p in "$port" "$api_port" "$bili_port" "$translate_port"; do
  if ss -lnt "( sport = :$p )" | grep -q ":$p"; then echo "test port $p is occupied" >&2; exit 1;fi
done
container="vr2-bili-auth-$$"
scratch="$(mktemp -d "$root/rust/target/bili-auth-test.XXXXXXXX")"
api_pid=""
cleanup() {
  if [[ -n "$api_pid" ]];then kill "$api_pid" 2>/dev/null || true; wait "$api_pid" 2>/dev/null || true;fi
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf -- "$scratch"
}
trap cleanup EXIT
docker run -d --rm --name "$container" --memory=512m \
    --publish "127.0.0.1:${port}:5432" \
    --env POSTGRES_PASSWORD=videoroll-test-20261121 \
    --env POSTGRES_DB=videoroll_test pgvector/pgvector:pg18 >/dev/null
for i in $(seq 1 60);do
  if docker logs "$container" 2>&1 | grep -q 'PostgreSQL init process complete; ready for start up' &&
     docker exec "$container" pg_isready -U postgres -d videoroll_test >/dev/null 2>&1;then break;fi
  sleep 1
done
docker exec -i "$container" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test \
    < rust/tests/bootstrap_legacy_for_integration.sql >/dev/null
export DATABASE_URL="postgresql://postgres:videoroll-test-20261121@127.0.0.1:${port}/videoroll_test"
export VIDEOROLL_V2_BIND="127.0.0.1:${api_port}"
export VIDEOROLL_V2_TEST_HTTP_BASE="http://127.0.0.1:${api_port}"
export VIDEOROLL_V2_TEST_BILIBILI_PORT="$bili_port"
export VIDEOROLL_V2_TEST_TRANSLATE_PORT="$translate_port"
export VIDEOROLL_V2_INDEX_ENDPOINT="http://127.0.0.1:${translate_port}/v1/chat/completions"
export VIDEOROLL_V2_ALLOW_TEST_BILIBILI_LOOPBACK=true
export VIDEOROLL_V2_TEST_BILIBILI_BASE="http://127.0.0.1:${bili_port}/"
export VIDEOROLL_V2_STORAGE_ROOT="$scratch"
export VIDEOROLL_V2_INTERNAL_TOKEN="v2-isolated-bili-auth-test-only-12345678"
export INTERNAL_API_SECRET="v2-isolated-bili-internal-secret"
export ADMIN_BOOTSTRAP_SECRET="v2-isolated-bili-admin-bootstrap"
binary="$root/rust/target/debug/videoroll-v2"
test -x "$binary"
"$binary" migrate
"$binary" api > "$scratch/api.log" 2>&1 &
api_pid=$!
for i in $(seq 1 50);do
  if curl -fsS "http://127.0.0.1:${api_port}/ready" >/dev/null;then break;fi
  sleep 0.2
done
curl -fsS "http://127.0.0.1:${api_port}/ready" >/dev/null
if [[ "${VIDEOROLL_V2_TEST_WS_ONLY:-false}" == "true" ]];then
  curl -fsS -H "x-videoroll-admin-bootstrap: $ADMIN_BOOTSTRAP_SECRET" -H 'content-type: application/json' \
    -d '{"password":"rust-test-secret-123"}' "$VIDEOROLL_V2_TEST_HTTP_BASE/api/auth/setup" >/dev/null
  /opt/videoroll/venvs/core/bin/python rust/tests/contract_parity_64.py \
    --ws-only --base "$VIDEOROLL_V2_TEST_HTTP_BASE" \
    --task-id "00000000-0000-0000-0000-000000000011" \
    --alert-id "00000000-0000-0000-0000-000000000011" \
    --out "$scratch/ws-debug.json"
  exit 0
fi
PYTHONPATH="$root/src" /opt/videoroll/venvs/core/bin/python \
    rust/tests/bilibili_account_v1_isolated.py
