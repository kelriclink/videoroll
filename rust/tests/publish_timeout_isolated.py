"""Real PostgreSQL timeout recovery; no worker or external publisher is started."""
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from uuid import uuid4
import psycopg

uri=os.environ["DATABASE_URL"]
assert urlsplit(uri).hostname=="127.0.0.1" and urlsplit(uri).path=="/videoroll_test"
def db():
    return psycopg.connect(uri)
def task(age=72,owner="rust"):
    t=uuid4()
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,updated_at) VALUES(%s,'local','own','publishing',%s,now()-(%s*interval '1 hour'))",(t,owner,age))
    return t
def job(t,state="submitting",age=72,receipt=None):
    j=uuid4()
    with db() as c:
        c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,external_id,updated_at,response_json) VALUES(%s,%s,'bilibili',%s,%s,now()-(%s*interval '1 hour'),%s::jsonb)",(j,t,state,receipt,age,json.dumps({"fixture":"preserve"})))
    return j
def tick(hours=None):
    env=os.environ.copy()
    env.pop("PUBLISHING_TASK_TIMEOUT_HOURS",None)
    if hours is not None: env["PUBLISHING_TASK_TIMEOUT_HOURS"]=str(hours)
    subprocess.run(["./target/debug/videoroll-v2","publish-timeout-tick"],env=env,check=True,timeout=60)
def task_state(t):
    with db() as c:
        return c.execute("SELECT status::text,error_code FROM tasks WHERE id=%s",(t,)).fetchone()
def receipt_state(j):
    with db() as c:
        return c.execute("SELECT state::text,response_json,external_id FROM publish_jobs WHERE id=%s",(j,)).fetchone()
def start(t):
    req=Request("http://127.0.0.1:18080/api/v2/workflows/start",
        data=json.dumps({"task_id":str(t)}).encode(),
        headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"],"content-type":"application/json"})
    try:
        with urlopen(req,timeout=10) as r:return r.status
    except HTTPError as e:return e.code

empty=task()
failed=task()
failure=job(failed,"failed")
uncertain=task()
stale=job(uncertain)
draft=task()
old_draft=job(draft,"draft")
unknown=task()
old_unknown=job(unknown,"unknown")
recent=task(24)
python=task(owner="python")
heartbeat=task()
recent_heartbeat=job(heartbeat)
leased=task()
active_lease=job(leased)
fresh=task()
new_job=job(fresh,age=2)
published=task()
verified=job(published,"published",receipt="BV-fixture")
batch=task()
batch_id=uuid4()
busy=task()
inbox=task()
inbox_job=job(inbox)
locked=task()
with db() as c:
    c.execute("UPDATE publish_jobs SET heartbeat_at=now() WHERE id=%s",(recent_heartbeat,))
    c.execute("UPDATE publish_jobs SET lease_owner='fixture',lease_until=now()+interval '1 hour' WHERE id=%s",(active_lease,))
    c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets) VALUES(%s,%s,'active','[{\"platform\":\"bilibili\"}]')",(batch_id,batch))
    c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES(%s,%s,1,'running','publish')",(uuid4(),busy))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'processing','{}')",(uuid4(),"publish-job:"+str(inbox_job)))
with db() as lock_conn:
    lock_conn.execute("SELECT id FROM tasks WHERE id=%s FOR UPDATE",(locked,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:tick(),range(2)))
    assert task_state(locked)==("publishing",None)
for t in (empty,failed):
    assert task_state(t)==("failed","PUBLISHING_TIMEOUT"),(t,task_state(t))
for t in (uncertain,draft,unknown):
    assert task_state(t)==("failed","PUBLISH_OUTCOME_UNKNOWN"),(t,task_state(t))
for j in (stale,old_draft):
    state,body,receipt=receipt_state(j)
    assert state=="unknown" and body["fixture"]=="preserve" and body["verify_platform_before_retry"] is True and receipt is None
assert receipt_state(old_unknown)==("unknown",{"fixture":"preserve"},None)
assert receipt_state(failure)==("failed",{"fixture":"preserve"},None)
for t in (recent,python,heartbeat,leased,fresh,published,batch,busy,inbox):
    assert task_state(t)==("publishing",None),(t,task_state(t))
assert receipt_state(verified)==("published",{"fixture":"preserve"},"BV-fixture")
for t in (uncertain,draft,unknown):
    assert start(t)==409,t
with db() as c:
    assert c.execute("SELECT count(*) FROM vr2_workflows WHERE task_id IN (%s,%s,%s)",(uncertain,draft,unknown)).fetchone()[0]==0
    assert c.execute("SELECT count(*) FROM outbox_events").fetchone()[0]==0
tick(hours=12)
assert task_state(recent)==("failed","PUBLISHING_TIMEOUT")
assert task_state(locked)==("failed","PUBLISHING_TIMEOUT")
for t in (python,heartbeat,leased,fresh,published,batch,busy,inbox):
    assert task_state(t)==("publishing",None),(t,task_state(t))
print("PASS: original timeout config, concurrent ticks, row lock, unknown receipt quarantine and restart fence, active/batch/receipt/Python protection; no external dispatch")
