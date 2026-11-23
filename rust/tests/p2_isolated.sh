#!/usr/bin/env bash
# Called only from smoke-isolated.sh. Requires its ephemeral Docker PostgreSQL
# and mock Bilibili owner API. Never contacts real Bilibili or UPOS.
set -euo pipefail
: "${container_name:?}" "${manual_dub_id:?}" "${manual_dub_job:?}" "${VIDEOROLL_V2_INTERNAL_TOKEN:?}"
p2_api_pid=""
cleanup_p2() {
  if [[ -n "$p2_api_pid" ]]; then kill "$p2_api_pid" 2>/dev/null || true;wait "$p2_api_pid" 2>/dev/null || true;fi
}
trap cleanup_p2 EXIT
pg() {
 docker exec "$container_name" psql -At -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c "$1"
}
p2_env() {
 VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI=true \
 VIDEOROLL_V2_BILIBILI_COOKIE='SESSDATA=video-roll-test' \
 VIDEOROLL_V2_BILIBILI_CSRF='1234567890123456' \
 VIDEOROLL_V2_BILIBILI_API_BASE='http://127.0.0.1:18084' \
 "$@"
}
VIDEOROLL_V2_BIND=127.0.0.1:18086 \
 VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI=true \
 VIDEOROLL_V2_BILIBILI_COOKIE='SESSDATA=video-roll-test' \
 VIDEOROLL_V2_BILIBILI_CSRF='1234567890123456' \
 VIDEOROLL_V2_BILIBILI_API_BASE='http://127.0.0.1:18084' \
 ./target/debug/videoroll-v2 api > "$repo_root/rust/target/v2-p2-api.log" 2>&1 &
p2_api_pid=$!
for _ in $(seq 1 60);do
 if curl -fsS http://127.0.0.1:18086/ready >/dev/null;then break;fi
 sleep 1
done
curl -fsS http://127.0.0.1:18086/ready >/dev/null
p2_hdr=(-H "x-videoroll-v2-internal-token: $VIDEOROLL_V2_INTERNAL_TOKEN" -H 'content-type: application/json')
p2_pub_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
p2_flow="$(python3 -c 'import uuid;print(uuid.uuid4())')"
p2_pub2="$(python3 -c 'import uuid;print(uuid.uuid4())')"
p2_flow2="$(python3 -c 'import uuid;print(uuid.uuid4())')"
# Preserve user's original publication: create a separate V2-owned,
# confirmed synthetic P1 in a disposable test DB only.
pg "INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration)
  SELECT '$p2_flow','$manual_dub_id',max(generation)+1,'succeeded','publish','{}'::jsonb
  FROM vr2_workflows WHERE task_id='$manual_dub_id';
" >/dev/null
# Insert synthetic single-part publication with a confirmed Bilibili receipt.
pg "INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,result)
  SELECT '$p2_pub_id',id,task_id,'publish',generation,'smoke:p2:$p2_pub_id','succeeded',
    '{\"native_bilibili\":true,\"confirmed\":true,\"parts\":1,\"aid\":34001,\"bvid\":\"BVtestoriginal\"}'::jsonb
  FROM vr2_workflows WHERE id='$p2_flow';
  UPDATE tasks SET status='published' WHERE id='$manual_dub_id';" >/dev/null
# Original P1 native subtitle sync was already confirmed for the single
# archive part. P2 append must preserve this proof while scheduling CID 2.
p2_srt_key="$(pg "SELECT storage_key FROM assets
  WHERE task_id='$manual_dub_id' AND kind='subtitle_srt'
  ORDER BY created_at DESC,id DESC LIMIT 1")"
test -n "$p2_srt_key"
pg "UPDATE operation_inbox SET bili_sync_source_key='$p2_srt_key',
   bili_sync_aid=34001,bili_sync_bvid='BVtestoriginal',bili_sync_parts=1,
   bili_sync_state='submitted',bili_sync_created_at=now(),
   bili_sync_submitted='{\"original-p1-proof\":{\"cid\":41001}}'::jsonb,
   updated_at=now() WHERE id='$p2_pub_id' AND stage='publish' AND runtime_engine='rust-v2';" >/dev/null
p2_body="{\"dubbing_job_id\":\"$manual_dub_job\",\"title\":\"【中配】Mock P2\"}"
p2_url="http://127.0.0.1:18086/api/v1/tasks/$manual_dub_id/publish_jobs/$p2_pub_id"
created="$(curl --silent --show-error --fail-with-body "${p2_hdr[@]}" -d "$p2_body" "$p2_url/append_p2")"
CREATED="$created" python3 - <<'PYCREATED'
import json,os
d=json.loads(os.environ['CREATED'])
assert d['state']=='queued' and d['attempt']==1,d
PYCREATED
same="$(curl --silent --show-error --fail-with-body "${p2_hdr[@]}" -d "$p2_body" "$p2_url/append_p2")"
test "$same" = "$created"
duplicate="$(curl -s -o /dev/null -w '%{http_code}' "${p2_hdr[@]}" \
 -d "{\"dubbing_job_id\":\"$manual_dub_job\",\"title\":\"Different P2\"}" "$p2_url/append_p2")"
test "$duplicate" = 409
# Upload is *explicitly* disabled against mock HTTP creator center. The
# pre-submit failure is durable and recoverable, with no external edit.
p2_env ./target/debug/videoroll-v2 p2-tick >/dev/null
test "$(pg "SELECT state FROM vr2_bilibili_p2_appends WHERE publish_job_id='$p2_pub_id'")" = failed
test "$(pg "SELECT count(*) FROM storage_object_refs WHERE owner_type='native_bilibili_p2' AND owner_id='$p2_pub_id'")" = 0
# Only failed pre-edit attempt can be explicitly requeued.
retry="$(curl --silent --show-error --fail-with-body "${p2_hdr[@]}" -d "$p2_body" "$p2_url/append_p2")"
RETRY="$retry" python3 - <<'PYRETRY'
import json,os
r=json.loads(os.environ['RETRY'])
assert r['state']=='queued' and r['attempt']==2,r
PYRETRY
test "$(pg "SELECT count(*) FROM storage_object_refs WHERE owner_type='native_bilibili_p2' AND owner_id='$p2_pub_id'")" = 1
# Simulate an already committed UPOS receipt to exercise the actual member
# archive edit protocol without contacting a real CDN.
pg "UPDATE vr2_bilibili_p2_appends SET state='uploaded',
  uploaded_filename='mock_p2_file',uploaded_cid=41002
  WHERE publish_job_id='$p2_pub_id';" >/dev/null
p2_env ./target/debug/videoroll-v2 p2-tick >/dev/null
test "$(pg "SELECT state FROM vr2_bilibili_p2_appends WHERE publish_job_id='$p2_pub_id'")" = published
test "$(pg "SELECT count(*) FROM storage_object_refs WHERE owner_type='native_bilibili_p2' AND owner_id='$p2_pub_id'")" = 0
test "$(pg "SELECT expected_parts||'|'||state||'|'||(submitted?'original-p1-proof')::text
    FROM vr2_bilibili_subtitle_syncs WHERE publish_job_id='$p2_pub_id'")" = '2|pending|true'
test "$(pg "SELECT count(*) FROM storage_object_refs WHERE owner_type='bilibili_subtitle' AND owner_id='$p2_pub_id' AND role='native_srt'")" = 1
p2_env ./target/debug/videoroll-v2 p2-tick >/dev/null
count="$(curl --silent --fail http://127.0.0.1:18084/__test_state | python3 -c 'import sys,json;print(json.load(sys.stdin)["p2_edit"])')"
test "$count" = 1
already="$(curl -s -o /dev/null -w '%{http_code}' "${p2_hdr[@]}" -d "$p2_body" "$p2_url/append_p2")"
test "$already" = 409
echo 'PASS: P1 native P2 append, upload failure safety, idempotent enqueue, retry, owner metadata preservation, exactly-once edit and GC references'

# A second fake P1 tests the ambiguous 503 boundary. It must NEVER post
# a duplicate edit, even if an operator tries to enqueue the same intent.
pg "INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration)
  SELECT '$p2_flow2','$manual_dub_id',max(generation)+1,'succeeded','publish','{}'::jsonb
  FROM vr2_workflows WHERE task_id='$manual_dub_id';
  INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,result)
  SELECT '$p2_pub2',id,task_id,'publish',generation,'smoke:p2:$p2_pub2','succeeded',
    '{\"native_bilibili\":true,\"confirmed\":true,\"parts\":1,\"aid\":34002,\"bvid\":\"BVtestambiguous\"}'::jsonb
  FROM vr2_workflows WHERE id='$p2_flow2';" >/dev/null
p2_url2="http://127.0.0.1:18086/api/v1/tasks/$manual_dub_id/publish_jobs/$p2_pub2"
curl -fsS "${p2_hdr[@]}" -d "$p2_body" "$p2_url2/append_p2" >/dev/null
pg "UPDATE vr2_bilibili_p2_appends SET state='uploaded',
    uploaded_filename='mock_p2_file',uploaded_cid=41002
    WHERE publish_job_id='$p2_pub2';" >/dev/null
p2_env ./target/debug/videoroll-v2 p2-tick >/dev/null
test "$(pg "SELECT state FROM vr2_bilibili_p2_appends WHERE publish_job_id='$p2_pub2'")" = unknown
p2_env ./target/debug/videoroll-v2 p2-tick >/dev/null
count="$(curl --silent --fail http://127.0.0.1:18084/__test_state | python3 -c 'import sys,json;print(json.load(sys.stdin)["p2_edit_ambiguous"])')"
test "$count" = 1
unknown_retry="$(curl -s -o /dev/null -w '%{http_code}' "${p2_hdr[@]}" -d "$p2_body" "$p2_url2/append_p2")"
test "$unknown_retry" = 409
status="$(curl -fsS "${p2_hdr[@]}" "$p2_url2/p2")"
STATUS="$status" python3 - <<'PYSTATUS'
import json,os
record=json.loads(os.environ['STATUS'])
assert record['state']=='unknown' and record['uploaded_cid']==41002,record
PYSTATUS
echo 'PASS: ambiguous Bilibili P2 edit stays unknown, prevents automatic replay or repeated append'
