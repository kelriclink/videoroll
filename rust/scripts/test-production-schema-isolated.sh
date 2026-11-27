#!/usr/bin/env bash
# Restore only an explicitly supplied schema-only snapshot and safe control
# allowlist into a new disposable database. Never connect to production.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${VIDEOROLL_TEST_SCHEMA_SNAPSHOT:?schema-only snapshot required}"
: "${VIDEOROLL_TEST_CONTROLS_SNAPSHOT:?nonsecret controls snapshot required}"
: "${VIDEOROLL_TEST_SCHEMA_BINARY:?explicit tested binary required}"
test -f "$VIDEOROLL_TEST_SCHEMA_BINARY"
test -f "$VIDEOROLL_TEST_SCHEMA_SNAPSHOT"
test -f "$VIDEOROLL_TEST_CONTROLS_SNAPSHOT"
if grep -qE '^(COPY |INSERT INTO )' "$VIDEOROLL_TEST_SCHEMA_SNAPSHOT"; then
  echo 'refusing row-bearing dump' >&2; exit 1
fi
name="vr2-schema-audit-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
volume="$name-db"
root="$(mktemp -d "${VIDEOROLL_TEST_SCRATCH_PARENT:-/tmp}/v2-schema-rehearsal.XXXXXX")"
cleanup(){
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf -- "$root"
  docker volume rm "$volume" >/dev/null
}
docker volume create --label io.videoroll.purpose=isolated-schema-audit "$volume" >/dev/null
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker run -d --rm --pull=never --name "$name" --memory=512m \
  --mount "type=volume,source=$volume,target=/var/lib/postgresql" \
  -p 127.0.0.1::5432 -e POSTGRES_PASSWORD=videoroll-isolated-schema \
  -e POSTGRES_DB=videoroll_test pgvector/pgvector:pg18 >/dev/null
ready=false
# pg_isready alone accepts the temporary initdb socket, even before the
# fixture database exists. Wait for the final PID 1 postmaster and an
# actual TCP query to our disposable database, not just a listening socket.
for i in $(seq 1 180); do
  if docker exec "$name" sh -c 'test "$(cat /proc/1/comm)" = postgres && PGPASSWORD="$POSTGRES_PASSWORD" psql -X -h 127.0.0.1 -U postgres -d videoroll_test -qAt -v ON_ERROR_STOP=1 -c "SELECT 1;"' 2>/dev/null | grep -qx 1; then
    ready=true; break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo 'isolated PostgreSQL failed final readiness check' >&2; exit 1
fi
docker exec "$name" psql -X -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c 'DROP SCHEMA public CASCADE;' >/dev/null
python3 - "$VIDEOROLL_TEST_SCHEMA_SNAPSHOT" <<'PY' | docker exec -i "$name" psql -X -v ON_ERROR_STOP=1 -U postgres -d videoroll_test >/dev/null
import sys
from pathlib import Path
schema=Path(sys.argv[1]).read_text()
assert schema.count("CREATE SCHEMA public;")==1
print(schema.replace("CREATE SCHEMA public;","CREATE SCHEMA public;\nCREATE EXTENSION vector WITH SCHEMA public;",1))
PY
port="$(docker port "$name" 5432/tcp | awk -F: '/^127.0.0.1:/ {print $NF}')"
[[ "$port" =~ ^[0-9]+$ ]]
export DATABASE_URL="postgresql://postgres:videoroll-isolated-schema@127.0.0.1:$port/videoroll_test"
export VIDEOROLL_V2_INTERNAL_TOKEN=isolated-schema-rehearsal-token-0123456789
export INTERNAL_API_SECRET=isolated-schema-rehearsal-service-secret
export VIDEOROLL_V2_STORAGE_ROOT="$root"
export VIDEOROLL_V2_STANDALONE=true VIDEOROLL_V2_NATIVE_OUTBOX_ONLY=true
export VIDEOROLL_V2_SOURCE_SCANNER=false VIDEOROLL_V2_REMOTE_RENDER=false
export VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI=false VIDEOROLL_V2_ENABLE_SAU_PUBLISH=false
export VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH=false VIDEOROLL_V2_COMPAT_WRITE=false
python="${VIDEOROLL_V2_TEST_PYTHON:-python3}"
case "${VIDEOROLL_TEST_SCHEMA_SCENARIO:-recovery}" in
  recovery) ;;
  native-stack|outbox|batch|render-events)
    # Existing repository-owned isolation tests; each gets a fresh database.
    export VIDEOROLL_TEST_SCHEMA_BINARY="$(realpath "$VIDEOROLL_TEST_SCHEMA_BINARY")"
    test -x "$VIDEOROLL_TEST_SCHEMA_BINARY"
    "$VIDEOROLL_TEST_SCHEMA_BINARY" migrate >/dev/null
    export PYTHONPATH="$(dirname "$PWD")/src"
    case "$VIDEOROLL_TEST_SCHEMA_SCENARIO" in
      native-stack) "$python" tests/native_deployment_isolated.py ;;
      outbox) "$python" tests/outbox_dispatcher_isolated.py ;;
      batch) "$python" tests/publish_batch_events_isolated.py ;;
      render-events) "$python" tests/render_events_isolated.py ;;
    esac
    exit 0 ;;
  *) echo "invalid isolated schema scenario" >&2; exit 1 ;;
esac
"$python" tests/production_schema_rehearsal.py seed
"$VIDEOROLL_TEST_SCHEMA_BINARY" migrate >/dev/null
"$python" tests/production_schema_rehearsal.py verify
"$python" tests/bulk_adoption_isolated.py
echo 'PASS: fresh isolated schema and bulk adoption'
