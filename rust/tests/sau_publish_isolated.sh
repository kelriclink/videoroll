#!/usr/bin/env bash
# Disposable PostgreSQL + local mock SAU. No production account or post.
set -euo pipefail
: "${container_name:?}" "${manual_dub_id:?}" "${VIDEOROLL_V2_STORAGE_ROOT:?}" "${VIDEOROLL_V2_INTERNAL_TOKEN:?}"
sau_api_pid=""
cleanup_sau() { if [[ -n "$sau_api_pid" ]];then kill "$sau_api_pid" 2>/dev/null||true;wait "$sau_api_pid" 2>/dev/null||true;fi; }
trap cleanup_sau EXIT
pg() { docker exec "$container_name" psql -At -v ON_ERROR_STOP=1 -U postgres -d videoroll_test -c "$1"; }
sau_env() {
 VIDEOROLL_V2_ENABLE_SAU_PUBLISH=true \
 VIDEOROLL_V2_SAU_STORAGE_ROOT="$VIDEOROLL_V2_STORAGE_ROOT" "$@"
}
VIDEOROLL_V2_BIND=127.0.0.1:18087 \
 VIDEOROLL_V2_ENABLE_SAU_PUBLISH=true \
 VIDEOROLL_V2_SAU_STORAGE_ROOT="$VIDEOROLL_V2_STORAGE_ROOT" \
 ./target/debug/videoroll-v2 api > "$repo_root/rust/target/v2-sau-publish-api.log" 2>&1 &
sau_api_pid=$!
for _ in $(seq 1 40);do
 if curl -fsS http://127.0.0.1:18087/ready >/dev/null 2>&1;then break;fi
 sleep 1
done
curl -fsS http://127.0.0.1:18087/ready >/dev/null
sau_hdr=(-H "x-videoroll-v2-internal-token: $VIDEOROLL_V2_INTERNAL_TOKEN" -H 'content-type: application/json')
base="http://127.0.0.1:18087/api/v1"
# Exercise the real Rust proxy against an HMAC-authenticated mock SAU:
# allow normal resource paths after rejecting authority/traversal substitution.
health="$(curl -fsS "${sau_hdr[@]}" "$base/settings/publish/social/health")"
HEALTH="$health" python3 - <<'PYSAUHEALTH'
import os,json
assert json.loads(os.environ["HEALTH"])=={"status":"ok","driver":"sau"}
PYSAUHEALTH
accounts="$(curl -fsS "${sau_hdr[@]}" "$base/settings/publish/social/accounts?platform=douyin")"
ACCOUNTS="$accounts" python3 - <<'PYSAUACCOUNTS'
import os,json
rows=json.loads(os.environ["ACCOUNTS"])
assert len(rows)==1 and rows[0]["platform"]=="douyin" and rows[0]["check_state"]=="valid",rows
PYSAUACCOUNTS
session="$(curl -fsS "${sau_hdr[@]}" -d '{"account_name":"Test"}' "$base/settings/publish/social/login-sessions/douyin")"
login_id="$(SESSION="$session" python3 -c 'import os,json;data=json.loads(os.environ["SESSION"]);assert data["state"]=="pending";print(data["id"])')"
curl -fsS "${sau_hdr[@]}" "$base/settings/publish/social/login-sessions/$login_id" | python3 -c 'import json,sys;assert json.load(sys.stdin)["state"]=="pending"'
curl -fsS "${sau_hdr[@]}" -X DELETE "$base/settings/publish/social/login-sessions/$login_id" | python3 -c 'import json,sys;assert json.load(sys.stdin)["state"]=="canceled"'
echo 'PASS: Rust SAU proxy health, filtered accounts, login session lifecycle and HMAC sidecar transport'
# Review controls are modified in the disposable DB only; the test checks
# that a skip_review request is NEVER accepted even when review is disabled.
curl -fsS "${sau_hdr[@]}" -X PUT -d '{"enabled":false,"blocked_words":[],"ai_rules":""}' \
 "$base/settings/review" >/dev/null
for platform in douyin xiaohongshu kuaishou;do
 curl -fsS "${sau_hdr[@]}" -X PUT -d '{"enabled":true}' \
  "$base/settings/publish/platforms/$platform" >/dev/null
done
social_settings="$(curl -fsS "${sau_hdr[@]}" "$base/settings/publish/platforms")"
SOCIAL_SETTINGS="$social_settings" python3 - <<'PY'
import os,json
s=json.loads(os.environ["SOCIAL_SETTINGS"])["platforms"]
assert all(s[p] for p in ["douyin","xiaohongshu","kuaishou"]),s
PY
video_key="$(pg "SELECT a.storage_key FROM assets a JOIN storage_objects o ON o.storage_key=a.storage_key AND o.state='live'
  WHERE a.task_id='$manual_dub_id' AND a.kind='video_final'
  ORDER BY a.created_at DESC LIMIT 1")"
test -n "$video_key"
test -s "$VIDEOROLL_V2_STORAGE_ROOT/$video_key"
make_body() {
 local platform="$1" account="$2" title="$3"
 PLATFORM="$platform" ACCOUNT="$account" TITLE="$title" VIDEO="$video_key" \
 python3 -c 'import os,json;print(json.dumps({"platform":os.environ["PLATFORM"],
 "account_id":os.environ["ACCOUNT"],"video_key":os.environ["VIDEO"],
 "meta":{"title":os.environ["TITLE"],"description":"Source-verified V2 fixture"},
 "platform_options":{}},ensure_ascii=False))'
}
douyin_account='1e2baeba-d95b-44dd-bf7b-aedb0ce5cd7a'
xhs_account='e9643df5-9c5a-4f86-b212-108f9fb0c095'
ks_account='ebd95c23-a7de-4b82-83f6-3eb3605d71f7'
dy_body="$(make_body douyin "$douyin_account" 'Rust V2 mock Douyin')"
task_publish="$base/tasks/$manual_dub_id/actions/publish"
deny_skip="$(curl -s -o /dev/null -w '%{http_code}' "${sau_hdr[@]}" -d \
  "$(python3 -c 'import json,sys;a=json.loads(sys.argv[1]);a["skip_review"]=True;print(json.dumps(a))' "$dy_body")" \
  "$task_publish")"
test "$deny_skip" = 400
created="$(curl -fsS "${sau_hdr[@]}" -d "$dy_body" "$task_publish")"
intent_id="$(CREATED="$created" python3 -c 'import os,json;a=json.loads(os.environ["CREATED"]);assert a["state"]=="queued";print(a["job_id"])')"
same="$(curl -fsS "${sau_hdr[@]}" -d "$dy_body" "$task_publish")"
test "$same" = "$created"
changed="$(curl -s -o /dev/null -w '%{http_code}' "${sau_hdr[@]}" -d \
  "$(make_body douyin "$douyin_account" 'New modified title')" "$task_publish")"
test "$changed" = 409
refs="$(pg "SELECT COUNT(*) FROM storage_object_refs WHERE owner_type='v2_sau_publish' AND owner_id='$intent_id'")"
test "$refs" = 1
sau_env ./target/debug/videoroll-v2 social-tick >/dev/null
state="$(pg "SELECT state FROM vr2_social_publish_intents WHERE id='$intent_id'")"
test "$state" = accepted
sau_env ./target/debug/videoroll-v2 social-tick >/dev/null
count="$(curl -fsS http://127.0.0.1:18085/__test_state | python3 -c 'import sys,json;print(json.load(sys.stdin)["douyin"])')"
test "$count" = 1
job_id="$(pg "SELECT sau_job_id FROM vr2_social_publish_intents WHERE id='$intent_id'")"
test "$job_id" = '04cd0a36-3813-414e-b9f5-3ee6ac71a0d5'
pg "INSERT INTO publish_jobs(id,task_id,platform,account_id,state,external_id,external_url)
    VALUES('$job_id','$manual_dub_id','douyin','$douyin_account','submitted',
           'douyin-mock-receipt','https://www.douyin.com/video/123456');" >/dev/null
sau_env ./target/debug/videoroll-v2 social-tick >/dev/null
test "$(pg "SELECT state FROM vr2_social_publish_intents WHERE id='$intent_id'")" = submitted
test "$(pg "SELECT COUNT(*) FROM storage_object_refs WHERE owner_type='v2_sau_publish' AND owner_id='$intent_id'")" = 0
history="$(curl -fsS "${sau_hdr[@]}" "$base/tasks/$manual_dub_id/publish_jobs")"
HISTORY="$history" INTENT="$intent_id" python3 - <<'PY'
import json,os
rows=json.loads(os.environ["HISTORY"])
assert any(v["id"]==os.environ["INTENT"] and v["platform"]=="douyin"
    and v["state"]=="submitted" and v["external_id"]=="douyin-mock-receipt"
    for v in rows),rows
PY
echo 'PASS: native SAU Douyin account validation, durable publish intent, no review bypass, dedupe, one sidecar dispatch, real receipt reconciliation and task-history status'

# Test Xiaohongshu ambiguous side effect: sidecar returned 503 after
# receiving request. The Rust outbox must never send it a second time.
xhs_body="$(make_body xiaohongshu "$xhs_account" simulate-timeout)"
xhs="$(curl -fsS "${sau_hdr[@]}" -d "$xhs_body" "$task_publish")"
xhs_id="$(XHS="$xhs" python3 -c 'import os,json;print(json.loads(os.environ["XHS"])["job_id"])')"
sau_env ./target/debug/videoroll-v2 social-tick >/dev/null
test "$(pg "SELECT state FROM vr2_social_publish_intents WHERE id='$xhs_id'")" = unknown
sau_env ./target/debug/videoroll-v2 social-tick >/dev/null
test "$(curl -fsS http://127.0.0.1:18085/__test_state | python3 -c 'import sys,json;print(json.load(sys.stdin)["ambiguous"])')" = 1
test "$(pg "SELECT COUNT(*) FROM storage_object_refs WHERE owner_type='v2_sau_publish' AND owner_id='$xhs_id'")" = 1
echo 'PASS: ambiguous SAU Xiaohongshu handoff cannot replay and retains media assets pending verification'

# A third platform account validates that Rust does not hardcode Douyin.
ks_body="$(make_body kuaishou "$ks_account" 'Rust V2 mock Kuaishou')"
ks="$(curl -fsS "${sau_hdr[@]}" -d "$ks_body" "$task_publish")"
ks_id="$(KS="$ks" python3 -c 'import os,json;print(json.loads(os.environ["KS"])["job_id"])')"
sau_env ./target/debug/videoroll-v2 social-tick >/dev/null
test "$(pg "SELECT state FROM vr2_social_publish_intents WHERE id='$ks_id'")" = accepted
test "$(curl -fsS http://127.0.0.1:18085/__test_state | python3 -c 'import sys,json;print(json.load(sys.stdin)["kuaishou"])')" = 1
echo 'PASS: native SAU Kuaishou account and durable browser-worker handoff'

# Original V1 publish_all wrapper must report partial results without claiming
# an atomic publish batch or replaying any already submitted SAU intent.
batch_body="$(DOUYIN="$douyin_account" XHS="$xhs_account" KS="$ks_account" VIDEO="$video_key" python3 - <<'PYBATCH'
import json,os
accounts={"douyin":os.environ["DOUYIN"],
          "xiaohongshu":os.environ["XHS"],"kuaishou":os.environ["KS"]}
titles={"douyin":"Rust V2 mock Douyin","xiaohongshu":"simulate-timeout",
        "kuaishou":"Rust V2 mock Kuaishou"}
print(json.dumps({"platforms":list(accounts),"account_ids":accounts,
  "video_key":os.environ["VIDEO"],"platform_meta":{
    p:{"title":titles[p],"description":"Source-verified V2 fixture"}
    for p in accounts},"platform_options":{}}))
PYBATCH
)"
batch_result="$(curl -fsS "${sau_hdr[@]}" -d "$batch_body" "$base/tasks/$manual_dub_id/actions/publish_all")"
BATCH="$batch_result" python3 - <<'PYBATCHRESULT'
import os,json
r=json.loads(os.environ["BATCH"])
assert r["batch_id"] is None,r
assert r["platform_count"]==3 and r["ok_count"]==2 and r["error_count"]==1,r
assert r["has_any_accepted"] and not r["all_accepted"] and not r["all_published"],r
assert r["results"]["douyin"]["state"]=="submitted",r
assert r["results"]["xiaohongshu"]["state"]=="unknown",r
assert r["results"]["kuaishou"]["state"]=="accepted",r
assert "xiaohongshu" in r["errors"],r
PYBATCHRESULT
bad_batch_code="$(curl -s -o /dev/null -w '%{http_code}' "${sau_hdr[@]}" \
  -d '{"platforms":["bilibili","douyin"]}' "$base/tasks/$manual_dub_id/actions/publish_all")"
test "$bad_batch_code" = 400
bad_batch_code="$(curl -s -o /dev/null -w '%{http_code}' "${sau_hdr[@]}" \
  -d '{"platforms":["douyin"],"skip_review":true}' "$base/tasks/$manual_dub_id/actions/publish_all")"
test "$bad_batch_code" = 400
counts="$(curl -fsS http://127.0.0.1:18085/__test_state)"
COUNTS="$counts" python3 - <<'PYCOUNTS'
import os,json
c=json.loads(os.environ["COUNTS"])
assert c["douyin"]==1 and c["kuaishou"]==1 and c["ambiguous"]==1,c
PYCOUNTS
echo 'PASS: V1 multi-SAU publish_all exposes per-platform partial outcomes, rejects hidden Bilibili targets and never repeats side effects'
