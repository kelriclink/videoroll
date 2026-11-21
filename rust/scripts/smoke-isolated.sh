#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
repo_root="$PWD"
export RUSTUP_HOME="$repo_root/.toolchains/rustup"
export CARGO_HOME="$repo_root/.toolchains/cargo"
container_name="vr2-smoke-$$"
api_pid=""
worker_pid=""
gateway_pid=""
cleanup() {
  if [[ -n "$worker_pid" ]]; then kill "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true; fi
  if [[ -n "$gateway_pid" ]]; then kill "$gateway_pid" 2>/dev/null || true; wait "$gateway_pid" 2>/dev/null || true; fi
  if [[ -n "$api_pid" ]]; then kill "$api_pid" 2>/dev/null || true; wait "$api_pid" 2>/dev/null || true; fi
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
if ss -lnt '( sport = :35432 )' | grep -q ':35432'; then echo 'test port 35432 occupied' >&2;exit 1;fi
docker run -d --rm --name "$container_name" --memory=512m   --publish 127.0.0.1:35432:5432   --env POSTGRES_PASSWORD=videoroll-test-20261121   --env POSTGRES_DB=videoroll_test pgvector/pgvector:pg18 >/dev/null
for i in $(seq 1 45); do
  if docker exec "$container_name" pg_isready -U postgres -d videoroll_test >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec -i "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test   < rust/tests/bootstrap_legacy_for_integration.sql >/dev/null
export DATABASE_URL="postgresql://postgres:videoroll-test-20261121@127.0.0.1:35432/videoroll_test"
export VIDEOROLL_V2_BIND="127.0.0.1:18080"
export VIDEOROLL_V2_STORAGE_ROOT="$repo_root/rust/target/v2-smoke-objects"
export VIDEOROLL_V2_INTERNAL_TOKEN="v2-local-test-only-0123456789abcdef"
export INTERNAL_API_SECRET="rust-local-test-internal-secret"
export ADMIN_BOOTSTRAP_SECRET="rust-local-test-bootstrap"
export VIDEOROLL_V2_SECURE_COOKIES="false"
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT"
cd rust
"$CARGO_HOME/bin/cargo" build --quiet
./target/debug/videoroll-v2 migrate
./target/debug/videoroll-v2 api > "$repo_root/rust/target/v2-smoke-api.log" 2>&1 &
api_pid=$!
for i in $(seq 1 40); do
 if curl --silent --fail http://127.0.0.1:18080/health >/dev/null; then break;fi
 sleep 1
done
curl --silent --fail http://127.0.0.1:18080/ready >/dev/null
status="$(curl --silent -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/api/v2/tasks)"
test "$status" = 401
hdr=(-H "x-videoroll-v2-internal-token: $VIDEOROLL_V2_INTERNAL_TOKEN" -H 'content-type: application/json')
first="$(curl --silent --fail-with-body "${hdr[@]}" -d '{"url":"https://youtu.be/0123456789A","source_license":"authorized"}' http://127.0.0.1:18080/api/v2/auto/youtube)"
second="$(curl --silent --fail-with-body "${hdr[@]}" -d '{"url":"https://www.youtube.com/watch?v=0123456789A","source_license":"authorized"}' http://127.0.0.1:18080/api/v2/auto/youtube)"
FIRST="$first" SECOND="$second" python3 - <<'PY'
import json,os
a=json.loads(os.environ["FIRST"]); b=json.loads(os.environ["SECOND"])
assert a["task"]["id"] == b["task"]["id"], "dedupe did not preserve task"
assert a["workflow"]["id"], "workflow not created in intake transaction"
assert a["deduped"] == False and b["deduped"] == True
assert b["workflow"] is None
print("PASS: atomic task/workflow creation and cross-URL deduplication")
PY
workflow="$(FIRST="$first" python3 -c 'import os,json;print(json.loads(os.environ["FIRST"])["workflow"]["id"])')"
curl --silent --fail-with-body "${hdr[@]}" -X POST "http://127.0.0.1:18080/api/v2/workflows/$workflow/pause" | grep -q '"state":"paused"'
curl --silent --fail-with-body "${hdr[@]}" -X POST "http://127.0.0.1:18080/api/v2/workflows/$workflow/resume" | grep -q '"state":"running"'
curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v2/workflows/$workflow/jobs" | grep -q '"stage":"download"'
echo 'PASS: API auth, workflow pause/resume, durable initial stage'
setup="$(curl --silent --fail-with-body \
  -H 'x-videoroll-admin-bootstrap: rust-local-test-bootstrap' \
  -H 'content-type: application/json' \
  -d '{"password":"rust-test-secret-123"}' \
  http://127.0.0.1:18080/api/auth/setup)"
echo "$setup" | grep -q '"password_set":true'
jar="$repo_root/rust/target/v2-login-cookies.txt"
curl --silent --fail-with-body -c "$jar" -H 'content-type: application/json' \
  -d '{"password":"rust-test-secret-123"}' \
  http://127.0.0.1:18080/api/auth/login | grep -q '"trusted":true'
curl --silent --fail-with-body -b "$jar" \
  http://127.0.0.1:18080/api/v2/tasks | grep -q '0123456789A'
echo 'PASS: native admin setup/login and browser cookie compatibility'



task_id="$(FIRST="$first" python3 -c 'import json,os;print(json.loads(os.environ["FIRST"])["task"]["id"])')"
media="$repo_root/rust/target/sample-video.mp4"
printf '0123456789ABCDEF' > "$media"
uploaded="$(curl --silent --fail-with-body "${hdr[@]}" \
  -H 'content-type: application/octet-stream' --data-binary "@$media" \
  "http://127.0.0.1:18080/api/v2/tasks/$task_id/assets/upload?kind=video_final&extension=mp4")"
asset_id="$(UPLOADED="$uploaded" python3 -c 'import json,os;print(json.loads(os.environ["UPLOADED"])["id"])')"
range="$(curl --silent --fail-with-body "${hdr[@]}" \
  -H 'range: bytes=0-3' \
  "http://127.0.0.1:18080/api/v2/assets/$asset_id/download")"
test "$range" = 0123
echo 'PASS: streaming artifact upload, register, and HTTP range download'
# The GC's deleting state is an acquisition fence, not merely a UI label.
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "UPDATE vr2_objects SET state='deleting' WHERE storage_key=(SELECT storage_key FROM assets WHERE id='$asset_id')" >/dev/null
if docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "INSERT INTO vr2_object_refs(storage_key,owner_type,owner_id,role) SELECT storage_key,'test','$task_id','race' FROM assets WHERE id='$asset_id'" >/dev/null 2>&1; then
  echo 'FAIL: allowed reference acquisition during garbage collection' >&2; exit 1
fi
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "UPDATE vr2_objects SET state='live' WHERE storage_key=(SELECT storage_key FROM assets WHERE id='$asset_id')" >/dev/null
echo 'PASS: garbage collector reference fence prevents the delete/register race'


# Simulate an unknown external publish outcome inside this isolated test DB.
# This does NOT contact Bilibili and is only a state-machine test.
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "UPDATE vr2_jobs SET stage='publish',state='unknown' WHERE workflow_id='$workflow';UPDATE vr2_workflows SET state='unknown',current_stage='publish' WHERE id='$workflow';" >/dev/null
job_id="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT id FROM vr2_jobs WHERE workflow_id='$workflow' AND stage='publish' LIMIT 1")"
blocked="$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}" \
  -d "{\"task_id\":\"$task_id\"}" http://127.0.0.1:18080/api/v2/workflows/start)"
test "$blocked" = 409
reconciled="$(curl --silent --fail-with-body "${hdr[@]}" \
  -d '{"accepted":true,"external_id":"BV0123456789","evidence":"Verified archive in external account history."}' \
  "http://127.0.0.1:18080/api/v2/jobs/$job_id/resolve-unknown")"
echo "$reconciled" | grep -q '"current_stage":"cleanup"'
curl --silent --fail-with-body "${hdr[@]}" \
  "http://127.0.0.1:18080/api/v2/tasks/$task_id" | grep -q '"status":"PUBLISHED"'
echo 'PASS: unknown publish blocks retries until manual verification'

second_task="$(curl --silent --fail-with-body "${hdr[@]}" \
  -d '{"url":"https://youtu.be/0123456789B"}' http://127.0.0.1:18080/api/v2/auto/youtube)"
cancel_id="$(SECOND_TASK="$second_task" python3 -c 'import json,os;print(json.loads(os.environ["SECOND_TASK"])["workflow"]["id"])')"
curl --silent --fail-with-body "${hdr[@]}" -X POST \
  "http://127.0.0.1:18080/api/v2/workflows/$cancel_id/cancel" | grep -q '"state":"canceled"'
echo 'PASS: workflow cancellation writes a durable terminal state'

# FULL CONTROL-PLANE SMOKE using mocked external workers only; no real platform or model calls.
fake_task="$(curl --silent --fail-with-body "${hdr[@]}" -d \
  '{"url":"https://youtu.be/0123456789C","source_license":"authorized"}' \
  http://127.0.0.1:18080/api/v2/auto/youtube)"
fake_task_id="$(FAKE_TASK="$fake_task" python3 -c 'import json,os;print(json.loads(os.environ["FAKE_TASK"])["task"]["id"])')"
fake_workflow_id="$(FAKE_TASK="$fake_task" python3 -c 'import json,os;print(json.loads(os.environ["FAKE_TASK"])["workflow"]["id"])')"
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$fake_task_id"
printf 'FAKE-MEDIA-NOT-REAL-MP4' > "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$fake_task_id/source.mp4"
fake_asset_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES('$fake_asset_id','$fake_task_id','video_raw','v2/raw/$fake_task_id/source.mp4','mock',23)" >/dev/null
python3 "$repo_root/rust/tests/mock_gateway.py" > "$repo_root/rust/target/v2-mock-gateway.log" 2>&1 &
gateway_pid=$!
VIDEOROLL_V2_WORKER_GATEWAY=http://127.0.0.1:18081 VIDEOROLL_V2_WORKERS=1 \
  ./target/debug/videoroll-v2 worker > "$repo_root/rust/target/v2-mock-worker.log" 2>&1 &
worker_pid=$!
done_task=""
for i in $(seq 1 70); do
  done_task="$(curl --silent --fail-with-body "${hdr[@]}" \
    "http://127.0.0.1:18080/api/v2/tasks/$fake_task_id")"
  if [[ "$done_task" == *'"status":"PUBLISHED"'* ]]; then break; fi
  sleep 1
done
if [[ "$done_task" != *'"status":"PUBLISHED"'* ]]; then
  tail -80 "$repo_root/rust/target/v2-mock-worker.log" >&2
  echo 'FAIL: six-stage mock control-plane task never reached PUBLISHED' >&2
  exit 1
fi
jobs="$(curl --silent --fail-with-body "${hdr[@]}" \
  "http://127.0.0.1:18080/api/v2/workflows/$fake_workflow_id/jobs")"
JOBS="$jobs" python3 - <<'PY2'
import json,os
rows=json.loads(os.environ["JOBS"])
assert len(rows)==6, rows
assert set(row['stage'] for row in rows)=={'download','subtitle','render','dubbing','publish','cleanup'},rows
assert all(row['state']=='succeeded' for row in rows),rows
print('PASS: six-stage Rust workflow completes with mock external workers')
PY2
