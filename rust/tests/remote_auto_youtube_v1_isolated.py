#!/usr/bin/env python3
"""Isolated original Python PBKDF2 + remote_api_requests + Rust public POST."""
import concurrent.futures
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg
from videoroll.apps.orchestrator_api.admin_auth_store import encode_password_hash
from videoroll.apps.orchestrator_api.remote_api import _request_hash
from videoroll.apps.orchestrator_api.schemas import RemoteAutoYouTubeRequest

db_url = os.environ["DATABASE_URL"]
assert db_url.endswith("/videoroll_test") and "127.0.0.1:" in db_url
base = "http://127.0.0.1:18080/api/v1"
admin_token = os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
remote_token = "isolated_remote_token_20261124_DO_NOT_USE"
def call(method="POST", path="/remote/auto/youtube", body=None, token=None, key=None, admin=False):
    headers={"Content-Type":"application/json"}
    if token is not None:headers["Authorization"]="Bearer "+token
    if admin:headers["x-videoroll-v2-internal-token"]=admin_token
    if key is not None:headers["Idempotency-Key"]=key
    data=json.dumps(body).encode() if body is not None else None
    req=Request(base+path,headers=headers,data=data,method=method)
    try:resp=urlopen(req,timeout=45)
    except HTTPError as exc:resp=exc
    with resp:
        raw=resp.read()
        try:payload=json.loads(raw)
        except ValueError:payload=raw.decode(errors="replace")
        assert remote_token not in str(payload)
        return resp.status,payload
def dbrow(key):
    with psycopg.connect(db_url) as db:
        row=db.execute("""SELECT token_hash,idempotency_key,request_hash,request_json,
                 status,response_json,lease_owner FROM remote_api_requests
                 WHERE idempotency_key=%s""",(key,)).fetchone()
        return row
def count_tasks():
    with psycopg.connect(db_url) as db:
        return db.execute("SELECT count(*) FROM ingested_videos WHERE platform='youtube'").fetchone()[0]

body={"url":"https://www.youtube.com/watch?v=Q1w2E3r4T5Y",
      "proof_url":"https://source.example.org/permission.txt"}
assert call("GET")[0]==410
assert call(body=body,token=remote_token,key="not-configured")[0]==403
# Must authenticate before attempting to parse the body.
assert call(body=None,token="invalid",key="unauthorized-empty-body")[0]==403
code,config=call("PUT","/settings/api",{"token":remote_token},admin=True)
assert code==200 and config["token_set"] and not "token_hash" in config,(code,config)
code,config=call("GET","/settings/api",admin=True)
assert config["token_set"] and "token_hash" not in config
assert call(body=body,admin=True,key="admin-not-valid")[0]==401
assert call(body=None,token="wrong-remote-token",key="unauthorized-empty-body")[0]==401
assert call(body=body,token="wrong-remote-token",key="wrong")[0]==401
assert call(body=body,token=remote_token)[0]==400
assert call(body=body,token=remote_token,key="")[0]==400
assert call(body={"url":"https://example.com/video"},token=remote_token,key="bad-url")[0]==400
assert call(body={"url":body["url"],"license":"bad"},token=remote_token,key="bad-license")[0]==400
assert call(body={"url":body["url"],"proof_url":"file:///etc/passwd"},token=remote_token,key="bad-proof")[0]==400
assert call(body=body,token=remote_token,key="x"*256)[0]==400
assert count_tasks()==0
print("PASS: GET 410, PBKDF2 token config, public Bearer only, key/URL/license/proof validation")

original=RemoteAutoYouTubeRequest.model_validate(body)
hash_expected=_request_hash(original)
# Original Pydantic drops unknown fields before canonical hashing/dispatch.
code,created=call(body={**body,"extra_unrecognized":"ignored"},
                  token=remote_token,key="remote-key-01")
assert code==200,(code,created)
assert created["deduped"] is False and created["task_id"] and created["pipeline_job_id"],created
assert created["source_id"]=="Q1w2E3r4T5Y"
row=dbrow("remote-key-01")
assert row[0]==hashlib.sha256(remote_token.encode()).hexdigest(),row
assert row[2]==hash_expected,(row[2],hash_expected)
assert row[3]=={} and row[4]=="completed" and row[5]==created and row[6] is None,row
assert count_tasks()==1
with psycopg.connect(db_url) as db:
    task=db.execute("SELECT runtime_owner,source_license,source_url,source_proof_url FROM tasks WHERE id=%s",
                    (created["task_id"],)).fetchone()
    assert task[0]=="rust" and task[1]=="authorized" and task[2]==body["url"]
    assert task[3]==body["proof_url"],task
    flows=db.execute("SELECT count(*) FROM vr2_workflows WHERE task_id=%s",(created["task_id"],)).fetchone()[0]
    assert flows==1
    assert db.execute("SELECT configuration FROM vr2_workflows WHERE task_id=%s",(created["task_id"],)).fetchone()[0]=={"legacy_auto_runtime_profile":True}
print("PASS: Python hash matches Rust canonical JSON; original durable reservation, Task and workflow created once; remote runtime policy not frozen")

code,replay=call(body=body,token=remote_token,key="remote-key-01")
assert code==200 and replay==created
assert call(body={**body,"auto_publish":True},token=remote_token,key="remote-key-01")[0]==409
assert count_tasks()==1
print("PASS: complete idempotency replay and payload mismatch conflict without new side effects")

# Simulate a Python-side completed reservation, without ever dispatching.
source="https://youtu.be/Z9Y8X7W6V5U"
key="python-completed-replay"
python_body=RemoteAutoYouTubeRequest(url=source)
stored={"task_id":created["task_id"],"pipeline_job_id":None,
        "deduped":True,"source_id":"Z9Y8X7W6V5U"}
with psycopg.connect(db_url) as db:
    db.execute("""INSERT INTO remote_api_requests
      (id,token_hash,idempotency_key,request_hash,request_json,status,response_json,lease_until)
      VALUES (%s,%s,%s,%s,'{}'::jsonb,'completed',%s::jsonb,now()+interval '24 hours')""",
      (uuid.uuid4(),row[0],key,_request_hash(python_body),json.dumps(stored)))
code,again=call(body={"url":source},token=remote_token,key=key)
assert code==200 and again==stored,(code,again)
assert count_tasks()==1
print("PASS: read/replay Python-created remote_api_requests records using exact original schema")

# In-flight, unknown and failed results must never be retried automatically.
for status in ("pending","failed"):
    key="original-"+status
    with psycopg.connect(db_url) as db:
        db.execute("""INSERT INTO remote_api_requests
        (id,token_hash,idempotency_key,request_hash,request_json,status,lease_owner,lease_until)
        VALUES(%s,%s,%s,%s,'{}'::jsonb,%s,'remote-api',now()-interval '1 day')""",
        (uuid.uuid4(),row[0],key,_request_hash(original),status))
    assert call(body=body,token=remote_token,key=key)[0]==409
    assert dbrow(key)[4]==status
assert count_tasks()==1
print("PASS: old pending/failed reservations cannot accidentally redispatch even after lease expiration")

# A different valid key for the same platform video still hits original ingest dedup.
code,duplicate=call(body=body,token=remote_token,key="remote-key-new")
assert code==200 and duplicate["deduped"] and duplicate["task_id"]==created["task_id"]
assert duplicate["pipeline_job_id"] is None
assert count_tasks()==1
print("PASS: original ingested_videos uniqueness persists beyond different remote idempotency keys")

parallel_body={"url":"https://www.youtube.com/watch?v=H1J2K3L4M5N"}
def concurrent_call(_):
    return call(body=parallel_body,token=remote_token,key="parallel-remote")
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    responses=list(pool.map(concurrent_call,range(4)))
# A replay may complete before the other requests enter the HTTP handler;
# either 409 pending or 200 identical replay is valid; only one workflow may exist.
assert all(status in (200,409) for status,_ in responses),responses
assert any(status==200 for status,_ in responses),responses
successful=[value for status,value in responses if status==200]
assert len({value["task_id"] for value in successful})==1,responses
assert dbrow("parallel-remote")[4]=="completed"
assert count_tasks()==2
print("PASS: concurrent public requests share one durable reservation and one physical workflow")

# A Python/admin token rotation invalidates the former token immediately.
new_token="rotated_remote_token_20261124_DO_NOT_USE"
code,rotated=call("PUT","/settings/api",{"token":new_token},admin=True)
assert code==200 and rotated["token_set"]
assert call(body=body,token=remote_token,key="old-token-revoked")[0]==401
assert call(body=body,token=new_token,key="new-token")[0]==200
print("PASS: token rotation and revocation use original settings; old bearer immediately invalid")

# Quota enforced with existing table; no Redis failure can bypass original request ledger.
digest=hashlib.sha256(new_token.encode()).hexdigest()
with psycopg.connect(db_url) as db:
    db.execute("""INSERT INTO remote_api_requests
       (id,token_hash,idempotency_key,request_hash,request_json,status)
       SELECT gen_random_uuid(),%s,'burst-'||n::text,%s,'{}'::jsonb,'completed'
       FROM generate_series(1,60) AS n""",(digest,hash_expected))
assert call(body=parallel_body,token=new_token,key="burst-limit")[0]==429
assert count_tasks()==2
print("PASS: database-backed per-token remote quota limits burst intake without Redis")

code,disabled=call("PUT","/settings/api",{"token":""},admin=True)
assert code==200 and disabled["token_set"] is False
assert call(body=body,token=new_token,key="no-token")[0]==403
print("PASS: remote entrypoint fails closed after token deletion; no secrets persisted")
