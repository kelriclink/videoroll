"""Original task list/detail compensation, with strict no-replay fences."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from uuid import uuid4
import psycopg

uri=os.environ["DATABASE_URL"]
assert urlsplit(uri).hostname=="127.0.0.1" and urlsplit(uri).path=="/videoroll_test"
def db():return psycopg.connect(uri)
def get(path):
    req=Request("http://127.0.0.1:18080/api/v1"+path,headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]})
    try:r=urlopen(req,timeout=20)
    except HTTPError as e:r=e
    with r:return r.status,json.loads(r.read())
def seed(status="failed",owner="rust",receipt="BV-fixture",state="published",response=None,platform="bilibili"):
    t,j=uuid4(),uuid4()
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,error_code,error_message) VALUES(%s,'local','own',%s,%s,'OLD_ERROR','old error')",(t,status,owner))
        c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,external_id,response_json,finished_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s::jsonb,now()-interval '2 hours',now()-interval '2 hours')",(j,t,platform,state,receipt,json.dumps(response or {"fixture":"keep"})))
    return t,j
def snapshot(table):
    with db() as c:return c.execute("SELECT to_jsonb(x) FROM "+table+" x ORDER BY 1").fetchall()
def read(t):
    with db() as c:return c.execute("SELECT status::text,error_code,error_message,updated_at FROM tasks WHERE id=%s",(t,)).fetchone()

detail=seed()
listing=seed(status="rendered")
filtered=seed(status="approved")
many=seed(platform="douyin",receipt="post-123")
canceled=seed(status="canceled")
paused=seed(status="canceled")
published=seed(status="published")
python=seed(owner="python")
unverified=seed(receipt=None)
blank=seed(receipt="  ")
unknown=seed(state="unknown")
submitted=seed(state="submitted")
unconfirmed=seed(response={"receipt_verified":False})
batch=seed()
historical_batch=seed()
leased=seed()
active=seed()
newer=seed()
pending=seed()
outbox=seed()
conflict=seed()
held=seed()
pending_download=seed()
with db() as c:
    c.execute("UPDATE tasks SET stopped_status='rendered' WHERE id=%s",(paused[0],))
    for item,active_batch in ((batch,True),(historical_batch,False)):
        b=uuid4()
        c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets) VALUES(%s,%s,'succeeded','[]')",(b,item[0]))
        if active_batch:c.execute("UPDATE tasks SET active_publish_batch_id=%s WHERE id=%s",(b,item[0]))
    c.execute("UPDATE publish_jobs SET lease_owner='fixture',lease_until=now()+interval '1 hour' WHERE id=%s",(leased[1],))
    for item,wstate in ((active,"running"),(newer,"succeeded")):
        c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES(%s,%s,1,%s,'publish')",(uuid4(),item[0],wstate))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'processing','{}')",(uuid4(),"publish-job:"+str(pending[1])))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'pending',%s::jsonb)",(uuid4(),"youtube-download:"+str(pending_download[0]),json.dumps({"task_id":str(pending_download[0])})))
    c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status) VALUES(%s,'publish.dispatch','publish_job',%s,'social_publisher.process_job','{}',%s,'pending')",(uuid4(),str(outbox[1]),"publish-job:"+str(outbox[1])))
    c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,external_id) VALUES(%s,%s,'douyin','unknown',NULL)",(uuid4(),conflict[0]))
    c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,external_id,finished_at) VALUES(%s,%s,'bilibili','published','BV-other',now()-interval '1 hour')",(uuid4(),many[0]))
tables=("publish_jobs","publish_batches","operation_inbox","outbox_events","pipeline_runs")
original={t:snapshot(t) for t in tables}
protected=[canceled,paused,published,python,unverified,blank,unknown,submitted,unconfirmed,batch,historical_batch,leased,active,newer,pending,outbox,conflict,pending_download]
before={x[0]:read(x[0]) for x in protected}
# Read route with held task or table lock should not wait or mutate.
with db() as lock:
    lock.execute("SELECT id FROM tasks WHERE id=%s FOR UPDATE",(held[0],))
    assert get("/tasks/"+str(held[0]))[1]["status"]=="FAILED"
    assert read(held[0])[0]=="failed"
with db() as lock:
    lock.execute("LOCK TABLE publish_jobs IN ROW EXCLUSIVE MODE")
    assert get("/tasks/"+str(detail[0]))[1]["status"]=="FAILED"
assert read(detail[0])[0]=="failed"
# Exact original list order: status filter is evaluated before compensation,
# then repaired rows leave that filtered page without refilling it.
status,body=get("/tasks?status=APPROVED&limit=1")
assert status==200 and body==[],(status,body)
assert read(filtered[0])[:3]==("published",None,None)
status,body=get("/tasks?status=RENDERED&limit=100")
assert status==200 and str(listing[0]) not in {x["id"] for x in body}
assert read(listing[0])[:3]==("published",None,None)
# Concurrent readers may observe pre-repair state, but next read converges.
with ThreadPoolExecutor(max_workers=4) as pool:
    results=list(pool.map(lambda _:get("/tasks/"+str(detail[0])),range(4)))
assert all(code==200 for code,_ in results)
for t,_ in (detail,many,held):
    code,body=get("/tasks/"+str(t))
    assert code==200 and body["status"]=="PUBLISHED" and body["error_code"] is None,(code,body)
    assert read(t)[:3]==("published",None,None)
    saved=read(t)
    assert get("/tasks/"+str(t))[0]==200 and read(t)==saved
for t,_ in protected:
    code,body=get("/tasks/"+str(t))
    if t==python[0]:assert code==404
    else:assert code==200,(code,body)
    assert read(t)==before[t],(t,read(t),before[t])
assert {t:snapshot(t) for t in tables}==original
print("PASS: original standalone published-task list/detail compensation, filter semantics, confirmed receipt and ownership fences, concurrent readers and locks, no external publication or ledger mutations")
