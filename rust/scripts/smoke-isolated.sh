#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
repo_root="$PWD"
if [[ -x "$repo_root/.toolchains/cargo/bin/cargo" ]]; then
  export RUSTUP_HOME="$repo_root/.toolchains/rustup"
  export CARGO_HOME="$repo_root/.toolchains/cargo"
else
  export CARGO_HOME="$HOME/.cargo"
fi
container_name="vr2-smoke-$$"
api_pid=""
worker_pid=""
gateway_pid=""
ai_pid=""
cleanup() {
  if [[ -n "$worker_pid" ]]; then kill "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true; fi
  if [[ -n "$ai_pid" ]]; then kill "$ai_pid" 2>/dev/null || true; wait "$ai_pid" 2>/dev/null || true; fi
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
export VIDEOROLL_V2_YT_DLP="$repo_root/rust/tests/mock_source_feed.sh"
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
  '{"url":"https://youtu.be/0123456789C","source_license":"authorized","configuration":{"auto_publish":true,"auto_publish_platforms":["bilibili"]}}' \
  http://127.0.0.1:18080/api/v2/auto/youtube)"
fake_task_id="$(FAKE_TASK="$fake_task" python3 -c 'import json,os;print(json.loads(os.environ["FAKE_TASK"])["task"]["id"])')"
fake_workflow_id="$(FAKE_TASK="$fake_task" python3 -c 'import json,os;print(json.loads(os.environ["FAKE_TASK"])["workflow"]["id"])')"
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$fake_task_id"
ffmpeg -hide_banner -nostdin -loglevel error -f lavfi -i 'color=c=blue:s=320x180:r=10' -t 1 -an -c:v libx264 -pix_fmt yuv420p -y "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$fake_task_id/source.mp4"
fake_asset_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES('$fake_asset_id','$fake_task_id','video_raw','v2/raw/$fake_task_id/source.mp4','mock',1024)" >/dev/null
python3 "$repo_root/rust/tests/mock_gateway.py" > "$repo_root/rust/target/v2-mock-gateway.log" 2>&1 &
gateway_pid=$!
python3 "$repo_root/rust/tests/mock_ai_server.py" > "$repo_root/rust/target/v2-mock-ai.log" 2>&1 &
ai_pid=$!
RUST_LOG=info VIDEOROLL_V2_ASR_ENDPOINT=http://127.0.0.1:18082/v1/audio/transcriptions VIDEOROLL_V2_ASR_MODEL=whisper-mock VIDEOROLL_V2_WORKER_GATEWAY=http://127.0.0.1:18081 VIDEOROLL_V2_WORKERS=1 \
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
for i in $(seq 1 35); do
  flow_state="$(curl --silent --fail-with-body "${hdr[@]}" \
    "http://127.0.0.1:18080/api/v2/workflows/$fake_workflow_id")"
  if [[ "$flow_state" == *'"state":"succeeded"'* ]]; then break;fi
  sleep 1
done
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

# End-to-end native Whisper-compatible ASR + actual subtitle burn-in via FFmpeg.
asr_task="$(curl --silent --fail-with-body "${hdr[@]}" -d \
  '{"url":"https://youtu.be/0123456789D","source_license":"authorized","configuration":{"auto_publish":true,"auto_publish_platforms":["bilibili"],"subtitle":{"translate_enabled":false},"render":{"burn_in":true,"video_codec":"h264"}}}' \
  http://127.0.0.1:18080/api/v2/auto/youtube)"
asr_task_id="$(ASR_TASK="$asr_task" python3 -c 'import json,os;print(json.loads(os.environ["ASR_TASK"])["task"]["id"])')"
asr_workflow="$(ASR_TASK="$asr_task" python3 -c 'import json,os;print(json.loads(os.environ["ASR_TASK"])["workflow"]["id"])')"
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$asr_task_id"
ffmpeg -hide_banner -nostdin -loglevel error \
  -f lavfi -i 'color=c=black:s=320x180:r=25' \
  -f lavfi -i 'sine=frequency=550:sample_rate=44100' \
  -t 2 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -y \
  "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$asr_task_id/source.mp4"
asr_asset_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "INSERT INTO assets(id,task_id,kind,storage_key) VALUES('$asr_asset_id','$asr_task_id','video_raw','v2/raw/$asr_task_id/source.mp4')" >/dev/null
asr_status=""
for i in $(seq 1 90); do
  asr_status="$(curl --silent --fail-with-body "${hdr[@]}" \
    "http://127.0.0.1:18080/api/v2/tasks/$asr_task_id")"
  if [[ "$asr_status" == *'"status":"PUBLISHED"'* ]]; then break; fi
  sleep 1
done
if [[ "$asr_status" != *'"status":"PUBLISHED"'* ]]; then
  tail -110 "$repo_root/rust/target/v2-mock-worker.log" >&2
  echo 'FAIL: native ASR+FFmpeg pipeline did not finish' >&2; exit 1
fi
test "$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT count(*) FROM assets WHERE task_id='$asr_task_id' AND kind='subtitle_srt'")" = 1
test "$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT count(*) FROM assets WHERE task_id='$asr_task_id' AND kind='video_final'")" = 1
srt_key="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT storage_key FROM assets WHERE task_id='$asr_task_id' AND kind='subtitle_srt' LIMIT 1")"
video_key="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT storage_key FROM assets WHERE task_id='$asr_task_id' AND kind='video_final' LIMIT 1")"
grep -q 'Hello from a Rust integration test' "$VIDEOROLL_V2_STORAGE_ROOT/$srt_key"
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=noprint_wrappers=1 "$VIDEOROLL_V2_STORAGE_ROOT/$video_key" | grep -q '^codec_name=h264'
echo 'PASS: native OpenAI-compatible ASR, SRT/ASS, and real FFmpeg subtitle burn-in'

# Retention / playout safety: force an expired retention period in the disposable
# database. The final asset is protected while a playout link still exists.
scheduled="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT count(*) FROM vr2_workdirs WHERE task_id='$asr_task_id' AND state='retained'")"
test "$scheduled" = 4
final_id="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT id FROM assets WHERE task_id='$asr_task_id' AND kind='video_final' LIMIT 1")"
link_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "INSERT INTO playout_asset_links(id,task_id,asset_id) VALUES('$link_id','$asr_task_id','$final_id')" >/dev/null
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "UPDATE vr2_objects SET retain_until=now()-interval '1 minute' WHERE storage_key LIKE 'v2/%' AND storage_key LIKE '%$asr_task_id%'; UPDATE vr2_workdirs SET due_at=now()-interval '1 minute' WHERE task_id='$asr_task_id';" >/dev/null
./target/debug/videoroll-v2 gc --limit 100 >/dev/null
test -f "$VIDEOROLL_V2_STORAGE_ROOT/$video_key"
test "$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT state FROM vr2_objects WHERE storage_key='$video_key'")" = "live"
echo 'PASS: playout-linked finished asset is protected from deferred GC'
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "DELETE FROM playout_asset_links WHERE id='$link_id'; UPDATE vr2_objects SET retain_until=now()-interval '1 minute' WHERE storage_key='$video_key';" >/dev/null
./target/debug/videoroll-v2 gc --limit 100 >/dev/null
test "$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT state FROM vr2_objects WHERE storage_key='$video_key'")" = "deleted"
test ! -f "$VIDEOROLL_V2_STORAGE_ROOT/$video_key"
echo 'PASS: verified terminal asset is reclaimed only after its last playout reference disappears'


# Real source CRUD / discovery persisted by the Rust API, fake yt-dlp only.
feed="$(curl --silent --fail-with-body "${hdr[@]}" -d \
 '{"source_type":"channel","source_id":"UC0000000000000000000","license":"authorized","auto_process":false,"scan_limit":20}' \
 http://127.0.0.1:18080/api/v2/youtube/sources)"
source_id="$(SOURCE="$feed" python3 -c 'import json,os;print(json.loads(os.environ["SOURCE"])["id"])')"
scan="$(curl --silent --fail-with-body "${hdr[@]}" -X POST \
 "http://127.0.0.1:18080/api/v2/youtube/sources/$source_id/scan")"
SCAN="$scan" python3 - <<'PYSRC'
import json,os
value=json.loads(os.environ["SCAN"])
assert value["discovered"]==2,value
assert value["created"]==2,value
assert value["pipelines_started"]==0,value
assert value["duplicated"]==0,value
PYSRC
scan="$(curl --silent --fail-with-body "${hdr[@]}" -X POST \
 "http://127.0.0.1:18080/api/v2/youtube/sources/$source_id/scan")"
SCAN="$scan" python3 - <<'PYSRC'
import json,os
value=json.loads(os.environ["SCAN"])
assert value["created"]==0,value
assert value["duplicated"]==2,value
PYSRC
echo 'PASS: YouTube channel source CRUD, non-autostart scan and durable video dedup'

# Versioned V2 overlay settings must never overwrite original v1 app_settings.
initial="$(curl --silent --fail-with-body "${hdr[@]}" \
  http://127.0.0.1:18080/api/v2/settings/auto)"
INITIAL="$initial" python3 - <<'PYSET'
import os,json
row=json.loads(os.environ["INITIAL"])
assert row["source"]=="v1_readonly",row
assert row["version"]==0,row
PYSET
new_settings="$(curl --silent --fail-with-body "${hdr[@]}" \
  -X PUT -d '{"expected_version":0,"value":{"translate_enabled":true,"auto_publish":false,"auto_publish_platforms":[],"asr_language":"en"}}' \
  http://127.0.0.1:18080/api/v2/settings/auto)"
NEW_SETTINGS="$new_settings" python3 - <<'PYSET'
import os,json
row=json.loads(os.environ["NEW_SETTINGS"])
assert row["source"]=="v2" and row["version"]==1,row
assert row["value"]["auto_publish"] is False,row
PYSET
conflict="$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}" \
  -X PUT -d '{"expected_version":0,"value":{"auto_publish":true}}' \
  http://127.0.0.1:18080/api/v2/settings/auto)"
test "$conflict" = 409
test "$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
  "SELECT count(*) FROM app_settings WHERE key='subtitle.auto_profile'")" = 0
echo 'PASS: V2 settings overlays are versioned and never overwrite v1 originals'

# Windows/Linux RenderWorker v1 wire protocol against the V2 coordinator.
# Only loopback, synthetic media and a dedicated ephemeral database are used.
kill "$worker_pid" 2>/dev/null || true
wait "$worker_pid" 2>/dev/null || true
VIDEOROLL_V2_REMOTE_RENDER=true VIDEOROLL_V2_WORKERS=1 \
  VIDEOROLL_V2_ASR_ENDPOINT=http://127.0.0.1:18082/v1/audio/transcriptions \
  VIDEOROLL_V2_ASR_MODEL=whisper-mock \
  VIDEOROLL_V2_WORKER_GATEWAY=http://127.0.0.1:18081 \
  ./target/debug/videoroll-v2 worker > "$repo_root/rust/target/v2-remote-worker.log" 2>&1 &
worker_pid=$!

enrollment="$(curl --silent --fail-with-body "${hdr[@]}" -d '{"label":"synthetic-remote-render-worker","ttl_minutes":30}' \
  http://127.0.0.1:18080/api/v2/render/enrollments)"
enrollment_token="$(ENROLLMENT="$enrollment" python3 -c 'import os,json;print(json.loads(os.environ["ENROLLMENT"])["token"])')"
enroll_body="$(TOKEN="$enrollment_token" python3 - <<'PYEN'
import json,os
print(json.dumps({
 "worker_key":"v2-test-worker","name":"V2 synthetic render node",
 "platform":"linux","architecture":"x86_64","version":"test-0.1",
 "protocol_version":1,"render_spec_versions":[1],
 "capabilities":{"encoders":["h264"]},"resources":{"cpu":1},"labels":{"test":True},
 "max_concurrency":1,"enrollment_token":os.environ["TOKEN"],
}))
PYEN
)"
enrolled="$(curl --silent --fail-with-body -H 'content-type: application/json' \
  -d "$enroll_body" http://127.0.0.1:18080/render-workers/v1/enroll)"
worker_id="$(ENROLLED="$enrolled" python3 -c 'import os,json;print(json.loads(os.environ["ENROLLED"])["worker"]["id"])')"
worker_credential="$(ENROLLED="$enrolled" python3 -c 'import os,json;print(json.loads(os.environ["ENROLLED"])["credential"])')"
replay="$(curl --silent -o /dev/null -w '%{http_code}' -H 'content-type: application/json' \
  -d "$enroll_body" http://127.0.0.1:18080/render-workers/v1/enroll)"
test "$replay" = 401
credential_header=(-H "authorization: Bearer $worker_credential" -H 'content-type: application/json')
curl --silent --fail-with-body "${credential_header[@]}" \
  -d '{"status":"online","resources":{},"capabilities":{},"active_execution_ids":[]}' \
  "http://127.0.0.1:18080/render-workers/v1/$worker_id/heartbeat" >/dev/null
remote_task="$(curl --silent --fail-with-body "${hdr[@]}" -d \
  '{"url":"https://youtu.be/0123456789G","source_license":"authorized","configuration":{"auto_publish":true,"auto_publish_platforms":["bilibili"],"subtitle":{"translate_enabled":false},"render":{"burn_in":true,"video_codec":"h264"}}}' \
  http://127.0.0.1:18080/api/v2/auto/youtube)"
remote_task_id="$(TASK="$remote_task" python3 -c 'import json,os;print(json.loads(os.environ["TASK"])["task"]["id"])')"
remote_workflow_id="$(TASK="$remote_task" python3 -c 'import json,os;print(json.loads(os.environ["TASK"])["workflow"]["id"])')"
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$remote_task_id"
ffmpeg -hide_banner -nostdin -loglevel error \
  -f lavfi -i 'color=c=black:s=320x180:r=25' \
  -f lavfi -i 'sine=frequency=550:sample_rate=44100' \
  -t 2 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -y \
  "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$remote_task_id/source.mp4"
remote_raw_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c \
  "INSERT INTO assets(id,task_id,kind,storage_key) VALUES('$remote_raw_id','$remote_task_id','video_raw','v2/raw/$remote_task_id/source.mp4')" >/dev/null
ready=0
for i in $(seq 1 65); do
  ready="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c \
    "SELECT count(*) FROM vr2_jobs WHERE workflow_id='$remote_workflow_id' AND stage='render' AND state='awaiting'")"
  if [[ "$ready" = 1 ]]; then break;fi
  sleep 1
done
if [[ "$ready" != 1 ]];then
  tail -65 "$repo_root/rust/target/v2-remote-worker.log" >&2
  echo 'FAIL: Remote render did not reach durable awaiting state' >&2
  exit 1
fi
claim="$(curl --silent --fail-with-body "${credential_header[@]}" \
  -d '{"available_slots":1,"accepted_transfer_modes":["http"],"available_encoders":["h264"]}' \
  "http://127.0.0.1:18080/render-workers/v1/$worker_id/claim")"
CLAIM="$claim" python3 - <<'PYCLAIM'
import os,json
body=json.loads(os.environ["CLAIM"])
assert body["execution"] and body["render_spec"],body
assert body["render_spec"]["schema_version"]==1,body
assert body["render_spec"]["mode"]=="burn_in",body
assert {x["role"] for x in body["render_spec"]["artifacts"]}=={"input","ass"},body
assert len(body["execution"]["fence_token"])==64,body
PYCLAIM
render_exec="$(CLAIM="$claim" python3 -c 'import os,json;print(json.loads(os.environ["CLAIM"])["execution"]["id"])')"
render_fence="$(CLAIM="$claim" python3 -c 'import os,json;print(json.loads(os.environ["CLAIM"])["execution"]["fence_token"])')"
ass_key="$(CLAIM="$claim" python3 -c 'import os,json;print(next(x["storage_key"] for x in json.loads(os.environ["CLAIM"])["render_spec"]["artifacts"] if x["role"]=="ass"))')"
render_input="$repo_root/rust/target/v2-remote-input.mp4"
curl --silent --fail-with-body -H "Authorization: Bearer $worker_credential" \
  "http://127.0.0.1:18080/render-workers/v1/executions/$render_exec/artifacts/input" \
  -o "$render_input"
ffprobe -v error -show_entries format=duration -of csv=p=0 "$render_input" >/dev/null
bad_fence="$(curl --silent -o /dev/null -w '%{http_code}' "${credential_header[@]}" \
  -d '{"fence_token":"invalid","progress":20,"metrics":{}}' \
  "http://127.0.0.1:18080/render-workers/v1/executions/$render_exec/heartbeat")"
test "$bad_fence" = 409
curl --silent --fail-with-body "${credential_header[@]}" \
  -d "{\"fence_token\":\"$render_fence\",\"progress\":35,\"metrics\":{\"pipeline\":\"render\"}}" \
  "http://127.0.0.1:18080/render-workers/v1/executions/$render_exec/progress" >/dev/null
remote_mp4="$repo_root/rust/target/v2-remote-render.mp4"
ffmpeg -hide_banner -nostdin -loglevel error -i "$render_input" \
  -vf "ass=$VIDEOROLL_V2_STORAGE_ROOT/$ass_key" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -y "$remote_mp4"
upload="$(curl --silent --fail-with-body -H "Authorization: Bearer $worker_credential" \
  -F "fence_token=$render_fence" -F "file=@$remote_mp4;type=video/mp4" \
  "http://127.0.0.1:18080/render-workers/v1/executions/$render_exec/output")"
remote_asset_id="$(UPLOAD="$upload" python3 -c 'import os,json;print(json.loads(os.environ["UPLOAD"])["id"])')"
curl --silent --fail-with-body -H "Authorization: Bearer $worker_credential" \
  "http://127.0.0.1:18080/render-workers/v1/executions/$render_exec/output?fence_token=$render_fence" \
  | grep -q "$remote_asset_id"
curl --silent --fail-with-body "${credential_header[@]}" \
  -d "{\"fence_token\":\"$render_fence\",\"output_asset_id\":\"$remote_asset_id\",\"output\":{\"device\":\"test\"}}" \
  "http://127.0.0.1:18080/render-workers/v1/executions/$render_exec/complete" >/dev/null
remote_final=""
for i in $(seq 1 70); do
  remote_final="$(curl --silent --fail-with-body "${hdr[@]}" \
    "http://127.0.0.1:18080/api/v2/tasks/$remote_task_id")"
  if [[ "$remote_final" == *'"status":"PUBLISHED"'* ]];then break;fi
  sleep 1
done
if [[ "$remote_final" != *'"status":"PUBLISHED"'* ]];then
  tail -80 "$repo_root/rust/target/v2-remote-worker.log" >&2
  echo 'FAIL: authenticated remote RenderWorker did not complete six-stage workflow' >&2
  exit 1
fi
echo 'PASS: isolated Windows RenderWorker v1-compatible enrollment, claim, artifact, fence, upload, complete'
