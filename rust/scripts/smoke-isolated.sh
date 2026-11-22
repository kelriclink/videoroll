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
legacy_pid=""
gateway_pid=""
ai_pid=""
bili_pid=""
cleanup() {
  if [[ -n "$worker_pid" ]]; then kill "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true; fi
  if [[ -n "$legacy_pid" ]]; then kill "$legacy_pid" 2>/dev/null || true; wait "$legacy_pid" 2>/dev/null || true; fi
  if [[ -n "$ai_pid" ]]; then kill "$ai_pid" 2>/dev/null || true; wait "$ai_pid" 2>/dev/null || true; fi
  if [[ -n "$bili_pid" ]]; then kill "$bili_pid" 2>/dev/null || true; wait "$bili_pid" 2>/dev/null || true; fi
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
export VIDEOROLL_V2_MOCK_YT_LOG="$repo_root/rust/target/v2-mock-ytdlp-args.log"
: > "$VIDEOROLL_V2_MOCK_YT_LOG"
export VIDEOROLL_V2_INDEX_ENDPOINT="http://127.0.0.1:18082/v1/chat/completions"
export VIDEOROLL_V2_COMPAT_URL="http://127.0.0.1:18083"
export VIDEOROLL_V2_PLAYOUT_ROOT="$repo_root/rust/target/playout-sandbox"
export VIDEOROLL_V2_YOUDUB_PYTHON="$(command -v python3)"
export VIDEOROLL_V2_YOUDUB_BRIDGE="$repo_root/rust/tests/mock_youdub_bridge.py"
export VIDEOROLL_V2_YOUDUB_RUNTIME="$repo_root/rust/target/mock-youdub-v2"
mkdir -p "$VIDEOROLL_V2_PLAYOUT_ROOT"
python3 "$repo_root/rust/tests/mock_legacy_api.py" > "$repo_root/rust/target/v2-mock-legacy.log" 2>&1 &
legacy_pid=$!
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT"
python3 "$repo_root/rust/tests/mock_bilibili_subtitles.py" > "$repo_root/rust/target/v2-bili-subs-mock.log" 2>&1 &
bili_pid=$!
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

# Original React TaskRead schema, isolated native V1 Rust routes.
v1_status="$(curl --silent --fail-with-body http://127.0.0.1:18080/api/v1/auth/status)"
V1_STATUS="$v1_status" python3 - <<'PYV1AUTH'
import json,os
status=json.loads(os.environ["V1_STATUS"])
assert status=={"password_set":True,"trusted":False},status
PYV1AUTH
v1_anonymous="$(curl --silent -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/api/v1/tasks)"
test "$v1_anonymous" = 401
v1_list="$(curl --silent --fail-with-body "${hdr[@]}" http://127.0.0.1:18080/api/v1/tasks?limit=2)"
v1_id="$(FIRST="$first" python3 -c 'import os,json;print(json.loads(os.environ["FIRST"])["task"]["id"])')"
v1_detail="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$v1_id")"
V1_LIST="$v1_list" V1_DETAIL="$v1_detail" python3 - <<'PYV1TASK'
import json,os
rows=json.loads(os.environ["V1_LIST"])
detail=json.loads(os.environ["V1_DETAIL"])
required={"id","source_type","source_url","source_license","source_proof_url","status",
          "stopped_status","priority","created_by","display_title","error_code",
          "error_message","retry_count","bilibili_upload","created_at","updated_at"}
assert len(rows)==1,rows
assert required <= set(rows[0]),rows
assert rows[0]==detail,rows
assert detail["source_type"]=="youtube",detail
assert detail["source_license"]=="authorized",detail
assert detail["status"]=="CREATED",detail
PYV1TASK
legacy_unknown="$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}" http://127.0.0.1:18080/api/v1/some-not-migrated-route)"
test "$legacy_unknown" = 404
echo 'PASS: native V1 TaskRead/list, auth boundaries and no silent fallback to Python'

settings_default="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/settings/youtube")"
SETTINGS_DEFAULT="$settings_default" python3 - <<'PYYTDEFAULT'
import json,os
v=json.loads(os.environ["SETTINGS_DEFAULT"])
assert not v["cookies_set"] and not v["cookies_enabled"],v
assert v["parallel_download_connections"]==8 and v["parallel_download_enabled"],v
PYYTDEFAULT
test "$(curl --silent -o /dev/null -w '%{http_code}'  "http://127.0.0.1:18080/api/v1/settings/youtube")" = 401
youtube_settings="$(curl --silent --fail-with-body "${hdr[@]}" -X PUT  -d '{"proxy":"http://127.0.0.1:18083","parallel_download_enabled":true,"parallel_download_connections":8,"compatibility_mode_enabled":true}'  "http://127.0.0.1:18080/api/v1/settings/youtube")"
YT_SETTINGS="$youtube_settings" python3 - <<'PYYTSETTINGS'
import json,os
v=json.loads(os.environ["YT_SETTINGS"])
assert v["proxy"]=="http://127.0.0.1:18083",v
assert v["parallel_download_connections"]==8 and v["compatibility_mode_enabled"],v
PYYTSETTINGS
cookie_upload="$(curl --silent --fail-with-body "${hdr[@]}" -X PUT  -d '{"cookies_txt":"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\ttest-cookie-secret","cookies_enabled":true}'  "http://127.0.0.1:18080/api/v1/settings/youtube")"
COOKIE_UPLOADED="$cookie_upload" python3 - <<'PYCOOKIESETTINGS'
import json,os
v=json.loads(os.environ["COOKIE_UPLOADED"])
assert v["cookies_enabled"] and v["cookies_set"] and v["cookies_count"]==1,v
assert v["cookies_has_auth"] and "test-cookie-secret" not in str(v),v
PYCOOKIESETTINGS
test "$(stat -c '%a' "$VIDEOROLL_V2_STORAGE_ROOT/.videoroll-private")" = 700
test "$(stat -c '%a' "$VIDEOROLL_V2_STORAGE_ROOT/.videoroll-private/youtube-cookies.txt")" = 600
! docker exec "$container_name" psql -At -U postgres -d videoroll_test -c  "SELECT value_json::text FROM vr2_settings WHERE key='youtube.settings'" | grep -q 'test-cookie-secret'
test "$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}" -X POST  -d '{"url":"http://127.0.0.1/admin"}'  "http://127.0.0.1:18080/api/v1/settings/youtube/test")" = 400
echo 'PASS: original YouTube settings native proxy/concurrency/compatibility and private Netscape cookies'


# Extract and cache genuine yt-dlp-compatible metadata for an owned task.
missing_meta="$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/youtube_meta")"
test "$missing_meta" = 404
initial_progress="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/youtube_download_progress")"
YT_PROGRESS="$initial_progress" python3 - <<'PYTPROGRESS'
import json,os
v=json.loads(os.environ["YT_PROGRESS"])
assert v["status"]=="queued" and not v["active"] and v["progress"]==0,v
PYTPROGRESS
fetched_yt="$(curl --silent --fail-with-body "${hdr[@]}" -X POST  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/actions/youtube_meta")"
FETCHED_YT="$fetched_yt" python3 - <<'PYFETCHEDYT'
import json,os
v=json.loads(os.environ["FETCHED_YT"])
assert v["metadata"]["title"]=="English title of test",v
assert v["metadata"]["duration"]==23,v
asset=v["metadata_asset"]
assert asset["kind"]=="metadata_json" and asset["storage_key"].startswith("v2/"),v
assert len(asset["sha256"])==64,v
PYFETCHEDYT
cached_yt="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/youtube_meta")"
CACHED_YT="$cached_yt" python3 - <<'PYCACHEDYT'
import json,os
v=json.loads(os.environ["CACHED_YT"])
assert v["uploader"]=="Test Channel" and v["description"]=="A synthetic tech explainer",v
PYCACHEDYT
echo 'PASS: original YouTube metadata read/fetch contract and V2 durable asset'

# Verify metadata extraction actually received settings from the control plane.
grep -q -- '--proxy' "$VIDEOROLL_V2_MOCK_YT_LOG"
grep -q -- 'http://127.0.0.1:18083' "$VIDEOROLL_V2_MOCK_YT_LOG"
grep -q -- '--cookies' "$VIDEOROLL_V2_MOCK_YT_LOG"
grep -q -- '--extractor-args' "$VIDEOROLL_V2_MOCK_YT_LOG"
grep -q -- 'youtube:player_client=default,web_embedded' "$VIDEOROLL_V2_MOCK_YT_LOG"
curl --silent --fail-with-body "${hdr[@]}" -X PUT  -d '{"cookies_txt":"","proxy":"","compatibility_mode_enabled":false}'  "http://127.0.0.1:18080/api/v1/settings/youtube" >/dev/null
test ! -e "$VIDEOROLL_V2_STORAGE_ROOT/.videoroll-private/youtube-cookies.txt"
echo 'PASS: metadata extractor uses configured proxy/cookie/compatibility and credentials are removed'


display_task="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$v1_id")"
DISPLAY_TASK="$display_task" python3 - <<'PYDISPLAYTASK'
import json,os
v=json.loads(os.environ["DISPLAY_TASK"])
assert v["display_title"]=="English title of test",v
PYDISPLAYTASK


draft="$(curl --silent --fail-with-body "${hdr[@]}"  -d '{"mode":"source","meta":{"typeid":21}}'  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_meta/draft")"
DRAFT="$draft" python3 - <<'PYDRAFT'
import os,json
m=json.loads(os.environ["DRAFT"])["meta"]
assert m["typeid"]==21 and m["title"]=="English title of test",m
assert m["source"]=="https://www.youtube.com/watch?v=0123456789A",m
assert m["copyright"]==2 and m["tags"]==["视频分享"],m
PYDRAFT
save_payload="$(DRAFT="$draft" python3 -c 'import os,json;print(json.dumps(json.loads(os.environ["DRAFT"])["meta"],ensure_ascii=False))')"
save_receipt="$(curl --silent --fail-with-body "${hdr[@]}" -X PUT -d "$save_payload"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_meta")"
SAVE_RECEIPT="$save_receipt" python3 - <<'PYSAVE'
import os,json
r=json.loads(os.environ["SAVE_RECEIPT"])
assert r["stored"] and r["key"].startswith("db:vr2_publish_meta:"),r
assert r["meta"]["tags"]==["视频分享"],r
PYSAVE
saved_meta="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_meta")"
SAVED_META="$saved_meta" SAVE_RECEIPT="$save_receipt" python3 - <<'PYREADMETA'
import os,json
assert json.loads(os.environ["SAVED_META"])==json.loads(os.environ["SAVE_RECEIPT"])["meta"]
PYREADMETA
invalid_meta="$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}" -X PUT  -d '{"title":"invalid","typeid":21,"tags":["tag"],"copyright":2,"topic_id":0}'  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_meta")"
test "$invalid_meta" = 400
revision="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c  "SELECT revision FROM vr2_publish_meta WHERE task_id='$v1_id'")"
test "$revision" = 1
echo 'PASS: original publish meta draft, validated transactional save and DB readback'

# Original review page and task detail editor: rejects before any model call.
review_settings="$(curl --silent --fail-with-body "${hdr[@]}" -X PUT  -d '{"enabled":true,"blocked_words":["English"],"ai_rules":"Reject unsafe materials."}'  "http://127.0.0.1:18080/api/v1/settings/review")"
REVIEW_SETTINGS="$review_settings" python3 - <<'PYREVIEWSET'
import json,os
r=json.loads(os.environ["REVIEW_SETTINGS"])
assert r["enabled"] and r["blocked_words"]==["English"],r
PYREVIEWSET
review_payload="$(SAVE_PAYLOAD="$save_payload" python3 -c 'import json,os;print(json.dumps({"meta":json.loads(os.environ["SAVE_PAYLOAD"])},ensure_ascii=False))')"
blocked_review="$(curl --silent --fail-with-body "${hdr[@]}" -d "$review_payload"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/actions/publish_review")"
BLOCKED_REVIEW="$blocked_review" python3 - <<'PYBLOCKEDREVIEW'
import json,os
r=json.loads(os.environ["BLOCKED_REVIEW"])
assert r["checked"] and not r["ok"] and r["review_mode"]=="blocked_words",r
assert r["matched_blocked_words"]==["English"],r
PYBLOCKEDREVIEW
stored_review="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_review")"
STORED_REVIEW="$stored_review" python3 - <<'PYSTOREDREVIEW'
import json,os
r=json.loads(os.environ["STORED_REVIEW"])
assert r["checked"] and not r["ok"],r
PYSTOREDREVIEW
curl --silent --fail-with-body "${hdr[@]}" -X PUT  -d '{"blocked_words":[]}'  "http://127.0.0.1:18080/api/v1/settings/review" >/dev/null
cleared_review="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_review")"
CLEARED_REVIEW="$cleared_review" python3 - <<'PYCLEAREDREVIEW'
import json,os
r=json.loads(os.environ["CLEARED_REVIEW"])
assert not r["checked"] and r["ok"] is None,r
PYCLEAREDREVIEW
echo 'PASS: original review settings/task review, deterministic prohibited words, and rule invalidation'



system_json="$(curl --silent --fail-with-body "${hdr[@]}" http://127.0.0.1:18080/api/v1/system/resources)"
SYSTEM_JSON="$system_json" python3 - <<'PYSYSTEM'
import json,os,datetime
v=json.loads(os.environ["SYSTEM_JSON"])
assert v["cpu"]["cores"]>=1 and v["cpu"]["percent"] is None,v
assert v["memory"]["total_bytes"]>0,v
assert v["memory"]["used_bytes"]>=0,v
assert v["memory"]["available_bytes"]>=0,v
assert v["intel_gpu"] is None,v
datetime.datetime.fromisoformat(v["sampled_at"])
PYSYSTEM
echo 'PASS: source-backed Linux system metrics without fabricated CPU/GPU utilization'


# Contract: original TaskNewPage can submit its local upload directly
# to native Rust without sending a video to the old Python backend.
local_task="$(curl --silent --fail-with-body "${hdr[@]}"   -d '{"source_type":"local","source_url":null,"source_license":"own","created_by":"web","priority":0}'   http://127.0.0.1:18080/api/v1/tasks)"
local_task_id="$(LOCAL_TASK="$local_task" python3 -c 'import os,json;print(json.loads(os.environ["LOCAL_TASK"])["id"])')"
LOCAL_TASK="$local_task" python3 - <<'PYLOCAL'
import json,os
task=json.loads(os.environ["LOCAL_TASK"])
assert task["source_type"]=="local" and task["status"]=="CREATED",task
assert task["source_license"]=="own" and task["created_by"]=="web",task
PYLOCAL
local_video="$repo_root/rust/target/v2-v1-local-upload.mp4"
export VIDEOROLL_V2_MOCK_YT_VIDEO="$local_video"
ffmpeg -hide_banner -nostdin -loglevel error   -f lavfi -i 'color=c=black:s=160x90:r=25'   -f lavfi -i 'sine=frequency=470:sample_rate=44100'   -t 1.5 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -y "$local_video"
local_asset="$(curl --silent --fail-with-body   -H "x-videoroll-v2-internal-token: $VIDEOROLL_V2_INTERNAL_TOKEN"   -F "file=@$local_video;filename=local-upload.mp4;type=video/mp4"   "http://127.0.0.1:18080/api/v1/tasks/$local_task_id/upload/video")"
LOCAL_ASSET="$local_asset" LOCAL_ID="$local_task_id" python3 - <<'PYLOCALASSET'
import json,os
asset=json.loads(os.environ["LOCAL_ASSET"])
assert asset["task_id"]==os.environ["LOCAL_ID"],asset
assert asset["kind"]=="video_raw" and asset["storage_key"].startswith("v2/"),asset
assert len(asset["sha256"])==64 and asset["size_bytes"]>1000,asset
PYLOCALASSET
local_status="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$local_task_id")"
LOCAL_STATUS="$local_status" python3 - <<'PYLOCALSTATUS'
import json,os
task=json.loads(os.environ["LOCAL_STATUS"])
assert task["status"]=="INGESTED",task
PYLOCALSTATUS
local_stored="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT count(*) FROM vr2_managed_tasks WHERE task_id='$local_task_id' AND origin='local_upload'")"
test "$local_stored" = 1
echo 'PASS: native V1 local TaskNewPage creation, verified streaming multipart, metadata and V2 ownership'
stop_local="$(curl --silent --fail-with-body "${hdr[@]}" -X POST   "http://127.0.0.1:18080/api/v1/tasks/$local_task_id/actions/stop")"
STOP_LOCAL="$stop_local" python3 - <<'PYSTOP'
import os,json
row=json.loads(os.environ["STOP_LOCAL"])
assert row["status"]=="CANCELED" and row["stopped_status"]=="INGESTED",row
PYSTOP
curl --silent --fail-with-body "${hdr[@]}" -X POST   "http://127.0.0.1:18080/api/v1/tasks/$local_task_id/actions/resume"   | grep -q '"status":"INGESTED"'
stop_first="$(curl --silent --fail-with-body "${hdr[@]}" -X POST   "http://127.0.0.1:18080/api/v1/tasks/$v1_id/actions/stop")"
STOP_FIRST="$stop_first" python3 - <<'PYSTOPFLOW'
import os,json
row=json.loads(os.environ["STOP_FIRST"])
assert row["status"]=="CANCELED" and row["stopped_status"]=="CREATED",row
PYSTOPFLOW
curl --silent --fail-with-body "${hdr[@]}"   "http://127.0.0.1:18080/api/v2/workflows/$workflow" | grep -q '"state":"paused"'
curl --silent --fail-with-body "${hdr[@]}" -X POST   "http://127.0.0.1:18080/api/v1/tasks/$v1_id/actions/resume" | grep -q '"status":"CREATED"'
curl --silent --fail-with-body "${hdr[@]}"   "http://127.0.0.1:18080/api/v2/workflows/$workflow" | grep -q '"state":"running"'
echo 'PASS: native V1 individual stop/resume maintains original TaskStatus and durable workflow generation'




# The original API compatibility surface is explicitly read-only and cannot
# forward V2 credentials, unauthenticated calls or unknown non-API routes.
legacy_read="$(curl --silent --fail-with-body "${hdr[@]}"   http://127.0.0.1:18080/api/compat/read)"
echo "$legacy_read" | grep -q '"read_only": true'
legacy_post_status="$(curl --silent -o /dev/null -w '%{http_code}'   -X POST "${hdr[@]}"   http://127.0.0.1:18080/api/compat/write)"
test "$legacy_post_status" = 409
legacy_anonymous="$(curl --silent -o /dev/null -w '%{http_code}'   http://127.0.0.1:18080/api/compat/read)"
test "$legacy_anonymous" = 401
echo 'PASS: isolated legacy API bridge allows authenticated reads only'




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

# Converted videos, assets and range streaming use original frontend
# response envelope but never serve non-V2 assets.
v1_videos="$(curl --silent --fail-with-body "${hdr[@]}" http://127.0.0.1:18080/api/v1/videos/converted?limit=10)"
v1_assets="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$asr_task_id/assets")"
v1_subtitles="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$asr_task_id/subtitle_jobs")"
V1_VIDEOS="$v1_videos" V1_ASSETS="$v1_assets" V1_SUBS="$v1_subtitles" ASR_TASK_ID="$asr_task_id" python3 - <<'PYV1VIDEO'
import json,os
task_id=os.environ["ASR_TASK_ID"]
items=json.loads(os.environ["V1_VIDEOS"])
video=next(x for x in items if x["task"]["id"]==task_id)
assert video["task"]["status"]=="PUBLISHED",video
assert video["final_asset"]["kind"]=="video_final",video
assert video["final_asset"]["storage_key"].startswith("v2/"),video
assets=json.loads(os.environ["V1_ASSETS"])
assert {a["kind"] for a in assets}>={"subtitle_srt","video_final"},assets
subs=json.loads(os.environ["V1_SUBS"])
assert len(subs)==1 and subs[0]["status"]=="succeeded" and subs[0]["progress"]==100,subs
PYV1VIDEO
asr_final_id="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT id FROM assets WHERE task_id='$asr_task_id' AND kind='video_final' LIMIT 1")"
status="$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}"   "http://127.0.0.1:18080/api/v1/tasks/$v1_id/assets/$asr_final_id/stream")"
test "$status" = 404
curl --silent --fail-with-body "${hdr[@]}" -H 'range: bytes=0-511'   "http://127.0.0.1:18080/api/v1/tasks/$asr_task_id/assets/$asr_final_id/stream"   | head -c 512 > "$repo_root/rust/target/v2-v1-stream-prefix.bin"
test "$(stat -c %s "$repo_root/rust/target/v2-v1-stream-prefix.bin")" = 512
range_headers="$(curl --silent --show-error --fail-with-body -D - -o /dev/null "${hdr[@]}" \
  -H 'range: bytes=0-1' \
  "http://127.0.0.1:18080/api/v1/tasks/$asr_task_id/assets/$asr_final_id/stream")"
echo "$range_headers" | grep -qi '^content-type: video/mp4'
echo 'PASS: native V1 converted videos, asset isolation and range streaming'

completed_progress="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$asr_task_id/youtube_download_progress")"
COMPLETE_YT="$completed_progress" python3 - <<'PYCOMPLETEYT'
import json,os
v=json.loads(os.environ["COMPLETE_YT"])
assert v["status"]=="completed" and v["progress"]==100 and not v["active"],v
assert v["downloaded_bytes"]>0 and v["total_bytes"]==v["downloaded_bytes"],v
PYCOMPLETEYT
frozen_code="$(curl --silent -o /dev/null -w '%{http_code}' "${hdr[@]}" -X PUT  -d '{"title":"不允许修改已投稿的稿件","typeid":21,"tags":["视频分享"]}'  "http://127.0.0.1:18080/api/v1/tasks/$asr_task_id/publish_meta")"
test "$frozen_code" = 409
echo 'PASS: legacy YouTube progress persisted from workflow and published metadata locked'



# ffplayout importer uses a flat translated filename and protects media.
playout_payload="$(printf '{"task_id":"%s","channel_id":1,"part":1,"title":"中文 测试 视频"}' "$asr_task_id")"
playout_result="$(curl --silent --fail-with-body "${hdr[@]}" -d "$playout_payload" http://127.0.0.1:18080/api/v2/playout/import)"
playout_id="$(PLAYOUT="$playout_result" python3 -c 'import os,json;print(json.loads(os.environ["PLAYOUT"])["id"])')"
playout_path="$(PLAYOUT="$playout_result" python3 -c 'import os,json;print(json.loads(os.environ["PLAYOUT"])["playout_path"])')"
PLAYOUT="$playout_result" python3 - <<'PYPLAY'
import os,json
row=json.loads(os.environ["PLAYOUT"])
assert row["status"]=="ready",row
assert row["relative_media_path"].startswith("VideoRoll/"),row
assert row["transfer_mode"] in ("hardlink","copy"),row
PYPLAY
test -s "$playout_path"
playout_again="$(curl --silent --fail-with-body "${hdr[@]}" -d "$playout_payload" http://127.0.0.1:18080/api/v2/playout/import)"
echo "$playout_again" | grep -q "$playout_id"
curl --silent --fail-with-body "${hdr[@]}" -X DELETE   "http://127.0.0.1:18080/api/v2/playout/links/$playout_id" | grep -q '"status":"detached"'
test -s "$playout_path"
echo 'PASS: ffplayout translated filename, verified media and safe detach'

# V2 lexical knowledge is always task-scoped or explicitly global.
knowledge="$(curl --silent --fail-with-body "${hdr[@]}" -d   '{"collection":"translation","scope":"global","kind":"term","title":"Rust ASR glossary","content":"Hello from a Rust integration test. The phrase Rust integration test remains consistent.","keywords":["Rust","integration","subtitle"],"language":"en","confidence":1.0}'   http://127.0.0.1:18080/api/v2/rag/knowledge)"
knowledge_id="$(KNOWLEDGE="$knowledge" python3 -c 'import json,os;print(json.loads(os.environ["KNOWLEDGE"])["id"])')"
hits="$(curl --silent --fail-with-body "${hdr[@]}"   -d '{"collection":"translation","query":"Hello from a Rust integration test","limit":8}'   http://127.0.0.1:18080/api/v2/rag/search)"
HITS="$hits" KNOWLEDGE_ID="$knowledge_id" python3 - <<'PYRAG'
import json,os
rows=json.loads(os.environ["HITS"])
assert any(v["id"]==os.environ["KNOWLEDGE_ID"] for v in rows),rows
assert rows[0]["score"]>0.1,rows
PYRAG
# Explicit local test-only model provider exercises the research Agent.
docker exec -i "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test >/dev/null <<'PSQL'
INSERT INTO app_settings(key,value_json) VALUES(
  'subtitle.model_providers',
  '{"providers":[{"id":"rag-test-provider","name":"RAG test",
    "api_type":"openai","base_url":"http://127.0.0.1:18082/v1",
    "models":[{"id":"rag-test-model","temperature":0.2}]}]}'
);
PSQL
rag_settings="$(curl --silent --fail-with-body "${hdr[@]}"   -X PUT -d '{"expected_version":0,"value":{"enabled":true,"collection":"translation","max_items":8,"max_context_chars":2000,"use_embedding":false,"research_enabled":true,"research_blocking":true,"research_provider_id":"rag-test-provider","research_model_id":"rag-test-model","max_research_steps":4}}'   http://127.0.0.1:18080/api/v2/settings/rag)"
echo "$rag_settings" | grep -q '"version":1'
echo 'PASS: scoped RAG knowledge storage, native lexical ranking and explicit research settings'

# Native official Index-Translate-compatible JSON numbering and missing-line repair.
index_task="$(curl --silent --fail-with-body "${hdr[@]}" -d   '{"url":"https://youtu.be/0123456789I","source_license":"authorized","configuration":{"auto_publish":true,"auto_publish_platforms":["bilibili"],"subtitle":{"translate_enabled":true,"translate_provider":"index_translate","translate_model_provider_id":"index_translate","translate_model_id":"Index-Translate-35B-A3B"}}}'   http://127.0.0.1:18080/api/v2/auto/youtube)"
index_task_id="$(INDEX_TASK="$index_task" python3 -c 'import json,os;print(json.loads(os.environ["INDEX_TASK"])["task"]["id"])')"
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$index_task_id"
ffmpeg -hide_banner -nostdin -loglevel error   -f lavfi -i 'color=c=black:s=320x180:r=25'   -f lavfi -i 'sine=frequency=550:sample_rate=44100'   -t 2 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -y   "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$index_task_id/source.mp4"
index_raw_asset="$(python3 -c 'import uuid;print(uuid.uuid4())')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c   "INSERT INTO assets(id,task_id,kind,storage_key) VALUES('$index_raw_asset','$index_task_id','video_raw','v2/raw/$index_task_id/source.mp4')" >/dev/null
index_state=""
for i in $(seq 1 90); do
  index_state="$(curl --silent --fail-with-body "${hdr[@]}"     "http://127.0.0.1:18080/api/v2/tasks/$index_task_id")"
  if [[ "$index_state" == *'"status":"PUBLISHED"'* || "$index_state" == *'"status":"FAILED"'* ]];then break;fi
  sleep 1
done
if [[ "$index_state" != *'"status":"PUBLISHED"'* ]];then
  tail -120 "$repo_root/rust/target/v2-mock-worker.log" >&2
  echo 'FAIL: native Index model translation pipeline did not finish' >&2; exit 1
fi
index_srt="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT storage_key FROM assets WHERE task_id='$index_task_id' AND kind='subtitle_srt' ORDER BY created_at DESC LIMIT 1")"
test -n "$index_srt"
test "$(grep -c '译文：' "$VIDEOROLL_V2_STORAGE_ROOT/$index_srt")" = 3
echo 'PASS: native Index-Translate API payload, numbered 3-cue mapping, missing-line repair'
index_workflow_id="$(INDEX_TASK="$index_task" python3 -c 'import json,os;print(json.loads(os.environ["INDEX_TASK"])["workflow"]["id"])')"
agent_runs="$(curl --silent --fail-with-body "${hdr[@]}"   "http://127.0.0.1:18080/api/v2/rag/agents/$index_workflow_id")"
AGENT_RUNS="$agent_runs" KNOWLEDGE_ID="$knowledge_id" python3 - <<'PYAGENT'
import json,os
rows=json.loads(os.environ["AGENT_RUNS"])
assert len(rows)==1,rows
assert rows[0]["state"]=="succeeded",rows
assert os.environ["KNOWLEDGE_ID"] in rows[0]["result"]["context"]["evidence_ids"],rows
PYAGENT
echo 'PASS: RAG evidence attached to translated workflow and persisted as auditable research'

# Deterministic YouDub mock: the real V2 adapter must create exactly one
# third-party task, import a valid P2, and bind the video to that task.
dubbing_task="$(curl --silent --fail-with-body "${hdr[@]}" -d  '{"url":"https://youtu.be/0123456789X","source_license":"authorized","configuration":{"auto_publish":true,"auto_publish_platforms":["bilibili"],"subtitle":{"translate_enabled":false},"dubbing":{"enabled":true}}}'  http://127.0.0.1:18080/api/v2/auto/youtube)"
dubbing_task_id="$(DUB_TASK="$dubbing_task" python3 -c 'import os,json;print(json.loads(os.environ["DUB_TASK"])["task"]["id"])')"
dubbing_workflow_id="$(DUB_TASK="$dubbing_task" python3 -c 'import os,json;print(json.loads(os.environ["DUB_TASK"])["workflow"]["id"])')"
mkdir -p "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$dubbing_task_id"
ffmpeg -hide_banner -nostdin -loglevel error   -f lavfi -i 'color=c=black:s=320x180:r=25'   -f lavfi -i 'sine=frequency=440:sample_rate=44100'   -t 2 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -y   "$VIDEOROLL_V2_STORAGE_ROOT/v2/raw/$dubbing_task_id/source.mp4"
dub_source_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c   "INSERT INTO assets(id,task_id,kind,storage_key) VALUES('$dub_source_id','$dubbing_task_id','video_raw','v2/raw/$dubbing_task_id/source.mp4')" >/dev/null
dub_final=""
for i in $(seq 1 70); do
  dub_final="$(curl --silent --fail-with-body "${hdr[@]}"     "http://127.0.0.1:18080/api/v2/tasks/$dubbing_task_id")"
  if [[ "$dub_final" == *'"status":"PUBLISHED"'* || "$dub_final" == *'"status":"FAILED"'* ]];then break;fi
  sleep 1
done
if [[ "$dub_final" != *'"status":"PUBLISHED"'* ]];then
  tail -65 "$repo_root/rust/target/v2-mock-worker.log" >&2
  echo 'FAIL: V2 YouDub adapter did not complete P2 path' >&2
  exit 1
fi
dub_video="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT storage_key FROM assets WHERE task_id='$dubbing_task_id' AND kind='video_dubbed' LIMIT 1")"
test -n "$dub_video"
test -s "$VIDEOROLL_V2_STORAGE_ROOT/$dub_video"
ffprobe -v error -show_entries format=duration -of csv=p=0   "$VIDEOROLL_V2_STORAGE_ROOT/$dub_video" >/dev/null
dub_record="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT model_job_id::text || '|' || model_status || '|' || source_video_key || '|' || source_srt_key || '|' || output_video_key FROM vr2_dubbing_runs WHERE task_id='$dubbing_task_id' AND workflow_id='$dubbing_workflow_id'")"
DUB_RECORD="$dub_record" DUB_VIDEO="$dub_video" python3 - <<'PYDUB'
import os,uuid
fields=os.environ["DUB_RECORD"].split("|")
assert len(fields)==5,fields
uuid.UUID(fields[0])
assert fields[1]=="succeeded",fields
assert fields[2].startswith("v2/raw/"),fields
assert fields[3].startswith("v2/sub/"),fields
assert fields[4]==os.environ["DUB_VIDEO"],fields
PYDUB
echo 'PASS: YouDub isolated adapter, deterministic model job, verified P2 video association'

v1_dub_jobs="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$dubbing_task_id/dubbing_jobs")"
v1_publish_jobs="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$dubbing_task_id/publish_jobs")"
V1_DUB_JOBS="$v1_dub_jobs" V1_PUBLISH_JOBS="$v1_publish_jobs" DUB_VIDEO="$dub_video" python3 - <<'PYV1HISTORY'
import json,os
dub=json.loads(os.environ["V1_DUB_JOBS"])
assert len(dub)==1 and dub[0]["status"]=="succeeded",dub
assert dub[0]["output"]["video_key"]==os.environ["DUB_VIDEO"],dub
pub=json.loads(os.environ["V1_PUBLISH_JOBS"])
assert len(pub)==1 and pub[0]["state"]=="published",pub
assert pub[0]["upload_progress"]==100,pub
PYV1HISTORY
echo 'PASS: native V1 dubbing and publish stage history adapters'


# Post-publication native subtitle outbox (test-only confirmed receipts).
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c   "UPDATE vr2_jobs SET result=COALESCE(result,'{}'::jsonb)||jsonb_build_object('native_bilibili',true,'aid',101,'bvid','BV1mockNativeTest','parts',1) WHERE task_id='$asr_task_id' AND stage='publish' AND state='succeeded'; UPDATE vr2_jobs SET result=COALESCE(result,'{}'::jsonb)||jsonb_build_object('native_bilibili',true,'aid',102,'bvid','BV1mockNativeTest','parts',1) WHERE task_id='$index_task_id' AND stage='publish' AND state='succeeded'; UPDATE vr2_jobs SET result=COALESCE(result,'{}'::jsonb)||jsonb_build_object('native_bilibili',true,'aid',103,'bvid','BV1mockNativeTest','parts',2) WHERE task_id='$dubbing_task_id' AND stage='publish' AND state='succeeded'" >/dev/null
bili_sync_env=(
  "VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI=true"
  "VIDEOROLL_V2_BILIBILI_SUBTITLES=true"
  "VIDEOROLL_V2_BILIBILI_COOKIE=SESSDATA=video-roll-test; bili_jct=1234567890123456"
  "VIDEOROLL_V2_BILIBILI_CSRF=1234567890123456"
  "VIDEOROLL_V2_BILIBILI_API_BASE=http://127.0.0.1:18084"
)
env "${bili_sync_env[@]}" ./target/debug/videoroll-v2 subtitles-tick >/dev/null
env "${bili_sync_env[@]}" ./target/debug/videoroll-v2 subtitles-tick >/dev/null
env "${bili_sync_env[@]}" ./target/debug/videoroll-v2 subtitles-tick >/dev/null
asr_publish_id="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT id FROM vr2_jobs WHERE task_id='$asr_task_id' AND stage='publish'")"
index_publish_id="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT id FROM vr2_jobs WHERE task_id='$index_task_id' AND stage='publish'")"
double_publish_id="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT id FROM vr2_jobs WHERE task_id='$dubbing_task_id' AND stage='publish'")"
bili_double="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v2/bilibili/subtitles/$double_publish_id")"
bili_ok="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v2/bilibili/subtitles/$asr_publish_id")"
bili_unknown="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v2/bilibili/subtitles/$index_publish_id")"
BILI_OK="$bili_ok" BILI_UNKNOWN="$bili_unknown" BILI_DOUBLE="$bili_double" python3 - <<'PYBILI'
import json,os
ok=json.loads(os.environ["BILI_OK"])
unknown=json.loads(os.environ["BILI_UNKNOWN"])
double=json.loads(os.environ["BILI_DOUBLE"])
assert double["state"]=="submitted" and len(double["submitted"])==2,double
assert ok["state"]=="submitted" and len(ok["submitted"])==1,ok
assert ok["source_checksum"] and len(ok["source_checksum"])==64,ok
assert unknown["state"]=="unknown" and unknown["inflight_cid"]==202,unknown
PYBILI
# A tick must not create another native draft while the status is uncertain.
env "${bili_sync_env[@]}" ./target/debug/videoroll-v2 subtitles-tick >/dev/null
count="$(curl --silent --fail-with-body http://127.0.0.1:18084/__test_state)"
COUNT="$count" python3 - <<'PYCOUNT'
import json,os
counts=json.loads(os.environ["COUNT"])
assert counts["presave_101"]==1 and counts["presave_102"]==1,counts
assert counts["presave_103_203"]==1 and counts["presave_103_204"]==1,counts
PYCOUNT
# Even an expired SRT must survive GC while its uncertain subtitle submission
# holds an independent reference, regardless of the video's published state.
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c   "UPDATE vr2_objects SET retain_until=now()-interval '1 minute' WHERE storage_key='$index_srt'" >/dev/null
./target/debug/videoroll-v2 gc --limit 100 >/dev/null
test -s "$VIDEOROLL_V2_STORAGE_ROOT/$index_srt"
curl --silent --fail-with-body "${hdr[@]}"   -d '{"cid":202,"submitted":true,"evidence":"Mock creator studio confirms the draft was accepted"}'   "http://127.0.0.1:18080/api/v2/bilibili/subtitles/$index_publish_id/resolve"   | grep -q '"reconciled":true'
env "${bili_sync_env[@]}" ./target/debug/videoroll-v2 subtitles-tick >/dev/null
resolved="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v2/bilibili/subtitles/$index_publish_id")"
RESOLVED="$resolved" python3 - <<'PYRESOLVE'
import json,os
row=json.loads(os.environ["RESOLVED"])
assert row["state"]=="submitted",row
assert list(row["submitted"].values())[0]["reconciled"],row
PYRESOLVE
count="$(curl --silent --fail-with-body http://127.0.0.1:18084/__test_state)"
COUNT="$count" python3 - <<'PYCOUNT'
import json,os
counts=json.loads(os.environ["COUNT"])
assert counts["presave_101"]==1 and counts["presave_102"]==1,counts
assert counts["presave_103_203"]==1 and counts["presave_103_204"]==1,counts
PYCOUNT
held="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT count(*) FROM vr2_object_refs WHERE owner_type='bilibili_subtitle' AND owner_id IN ('$asr_publish_id','$index_publish_id','$double_publish_id')")"
test "$held" = 0
echo 'PASS: Bilibili native subtitles, per-CID retry fencing, unknown reconciliation and SRT resource release'





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
# The detached audit row survives; its foreign key is safely null,
# and the ffplayout media file is never deleted by ordinary source GC.
historical="$(curl --silent --fail-with-body "${hdr[@]}"   "http://127.0.0.1:18080/api/v2/playout/links/$playout_id")"
HISTORICAL="$historical" python3 - <<'PYGC'
import json,os
row=json.loads(os.environ["HISTORICAL"])
assert row["status"]=="detached",row
assert row["asset_id"] is None,row
assert row["source_asset_id"] is not None,row
PYGC
test -s "$playout_path"
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

# A manually created YouTube task has no runnable worker until explicitly
# asked to start. A V1 page auto-create returns the exact legacy envelope.
manual_src="$(curl --silent --fail-with-body "${hdr[@]}"   -d '{"url":"https://www.youtube.com/watch?v=0123456789Q","license":"authorized","proof_url":"https://example.org/license"}'   "http://127.0.0.1:18080/api/v1/youtube/ingest")"
MANUAL_SRC="$manual_src" python3 - <<'PYMANUAL'
import json,os
data=json.loads(os.environ["MANUAL_SRC"])
assert data["task_id"] and data["source_id"]=="0123456789Q",data
assert data["deduped"]==False,data
PYMANUAL
manual_id="$(MANUAL_SRC="$manual_src" python3 -c 'import json,os;print(json.loads(os.environ["MANUAL_SRC"])["task_id"])')"
manual_job_count="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test -c   "SELECT count(*) FROM vr2_jobs WHERE task_id='$manual_id'")"
test "$manual_job_count" = 0
manual_task="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v1/tasks/$manual_id")"
MANUAL_TASK="$manual_task" python3 - <<'PYMANUALTASK'
import json,os
data=json.loads(os.environ["MANUAL_TASK"])
assert data["source_type"]=="youtube" and data["status"]=="CREATED",data
assert data["source_proof_url"]=="https://example.org/license",data
PYMANUALTASK
auto_src="$(curl --silent --fail-with-body "${hdr[@]}"   -d '{"url":"https://youtu.be/0123456789Y","license":"authorized","auto_publish":false}'   "http://127.0.0.1:18080/api/v1/auto/youtube")"
AUTO_SRC="$auto_src" python3 - <<'PYAUTO'
import json,os
data=json.loads(os.environ["AUTO_SRC"])
assert data["task_id"] and data["pipeline_job_id"] and not data["deduped"],data
assert data["source_id"]=="0123456789Y",data
PYAUTO
parity="$(curl --silent --fail-with-body "${hdr[@]}" "http://127.0.0.1:18080/api/v2/parity")"
PARITY="$parity" python3 - <<'PYPARITY'
import json,os
d=json.loads(os.environ["PARITY"])
assert d["legacy_route_declarations"]==217,d
assert d["native_v1_adapters"]>=38,d
assert d["fully_verified_legacy_routes"]==0 and d["all_features_migrated"] is False,d
assert d["frontend_url_references"]>=190,d
PYPARITY
echo 'PASS: original TaskNewPage YouTube manual/auto intake, conservative autopublish and API parity inventory'

# Test the *actual AI review request*, not a mocked stored approval. This
# happens after all other running flows so the model switch cannot affect ASR.
new_auto_profile="$(curl --silent --fail-with-body "${hdr[@]}" -X PUT  -d '{"expected_version":1,"value":{"translate_model_provider_id":"rag-test-provider","translate_model_id":"rag-test-model","auto_publish":false}}'  "http://127.0.0.1:18080/api/v2/settings/auto")"
NEW_AUTO_PROFILE="$new_auto_profile" python3 - <<'PYAUTOREVIEW'
import json,os
assert json.loads(os.environ["NEW_AUTO_PROFILE"])["version"]==2
PYAUTOREVIEW
ai_approved="$(curl --silent --fail-with-body "${hdr[@]}" -d "$review_payload"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/actions/publish_review")"
AI_APPROVED="$ai_approved" python3 - <<'PYAIAPPROVED'
import json,os
r=json.loads(os.environ["AI_APPROVED"])
assert r["checked"] and r["ok"] is True and r["review_mode"]=="ai",r
assert r["reason"]=="mock-approved",r
PYAIAPPROVED
saved_review="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_review")"
SAVED_REVIEW="$saved_review" python3 - <<'PYREADAI'
import json,os
r=json.loads(os.environ["SAVED_REVIEW"])
assert r["checked"] and r["ok"] is True,r
PYREADAI
rejected_payload="$(SAVE_PAYLOAD="$save_payload" python3 -c 'import json,os;m=json.loads(os.environ["SAVE_PAYLOAD"]);m["title"]="REJECT unsafe claim";print(json.dumps({"meta":m}))')"
ai_rejected="$(curl --silent --fail-with-body "${hdr[@]}" -d "$rejected_payload"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/actions/publish_review")"
AI_REJECTED="$ai_rejected" python3 - <<'PYAIREJECTED'
import json,os
r=json.loads(os.environ["AI_REJECTED"])
assert r["checked"] and r["ok"] is False and r["review_mode"]=="ai",r
assert r["reason"]=="mock-rejected",r
PYAIREJECTED
stale_review="$(curl --silent --fail-with-body "${hdr[@]}"  "http://127.0.0.1:18080/api/v1/tasks/$v1_id/publish_review")"
STALE_REVIEW="$stale_review" python3 - <<'PYSTALEREVIEW'
import json,os
r=json.loads(os.environ["STALE_REVIEW"])
assert not r["checked"] and r["ok"] is None,r
PYSTALEREVIEW
echo 'PASS: actual OpenAI-compatible AI review request, approval, rejection, fingerprints and stale-result fencing'

# Drive the actual Download stage through a slow synthetic yt-dlp child.
# Its numeric progress must reach the original frontend API while still
# running, and the database must never store raw/cookie payload strings.
live_download="$(curl --silent --fail-with-body "${hdr[@]}"  -d '{"url":"https://youtu.be/0123456789Z","source_license":"authorized","configuration":{"auto_publish":false}}'  "http://127.0.0.1:18080/api/v2/auto/youtube")"
live_task_id="$(LIVE_DL="$live_download" python3 -c 'import json,os;print(json.loads(os.environ["LIVE_DL"])["task"]["id"])')"
live_workflow_id="$(LIVE_DL="$live_download" python3 -c 'import json,os;print(json.loads(os.environ["LIVE_DL"])["workflow"]["id"])')"
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d videoroll_test  -c "UPDATE tasks SET priority=100 WHERE id='$live_task_id'" >/dev/null
live_state=""
for i in $(seq 1 90); do
  live_state="$(curl --silent --fail-with-body "${hdr[@]}"     "http://127.0.0.1:18080/api/v1/tasks/$live_task_id/youtube_download_progress")"
  if LIVE_STATE="$live_state" python3 -c 'import json,os,sys;x=json.loads(os.environ["LIVE_STATE"]);sys.exit(0 if x["active"] and x["progress"]==20 and x["downloaded_bytes"]==2000 and x["speed_bytes_per_second"]==2000.0 else 1)';then
    break
  fi
  sleep 1
done
LIVE_STATE="$live_state" python3 - <<'PYLIVEDL'
import json,os
d=json.loads(os.environ["LIVE_STATE"])
assert d["active"] and d["status"]=="downloading" and d["progress"]==20,d
assert d["downloaded_bytes"]==2000 and d["total_bytes"]==10000,d
assert d["eta_seconds"]==3 and d["speed_bytes_per_second"]==2000.,d
PYLIVEDL
done_progress=""
for i in $(seq 1 35); do
  done_progress="$(curl --silent --fail-with-body "${hdr[@]}"    "http://127.0.0.1:18080/api/v1/tasks/$live_task_id/youtube_download_progress")"
  if DONE_PROGRESS="$done_progress" python3 -c 'import json,os,sys;x=json.loads(os.environ["DONE_PROGRESS"]);sys.exit(0 if x["status"]=="completed" and x["progress"]==100 else 1)'; then break;fi
  sleep 1
done
DONE_PROGRESS="$done_progress" python3 - <<'PYDONEPROGRESS'
import json,os
d=json.loads(os.environ["DONE_PROGRESS"])
assert d["status"]=="completed" and d["progress"]==100,d
assert not d["active"] and d["total_bytes"]==d["downloaded_bytes"] and d["total_bytes"]>1000,d
assert d["speed_bytes_per_second"] is None and d["eta_seconds"] is None,d
PYDONEPROGRESS
grep -q -- '--concurrent-fragments' "$VIDEOROLL_V2_MOCK_YT_LOG"
grep -q -- 'http,https:aria2c' "$VIDEOROLL_V2_MOCK_YT_LOG"
lease_progress="$(docker exec "$container_name" psql -At -U postgres -d videoroll_test  -c "SELECT downloaded_bytes::text||'|'||percent::text FROM vr2_download_progress WHERE task_id='$live_task_id'")"
test -n "$lease_progress"
curl --silent --fail-with-body "${hdr[@]}" -X POST  "http://127.0.0.1:18080/api/v2/workflows/$live_workflow_id/cancel" >/dev/null
echo 'PASS: isolated yt-dlp child updates fenced real-time progress, aria2c HTTP-only flags, and completed media receipt'
