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

# A maintenance scanner must not run a second page while another session
# owns the scan gate. Closing the session releases it even without unlock.
gate_key="videoroll:v2:publish-timeout-scan"
gate_task=task()
with db() as gate:
    gate.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))",(gate_key,))
    gate.commit()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _:tick(),range(3)))
    assert task_state(gate_task)==("publishing",None)
tick()
assert task_state(gate_task)==("failed","PUBLISHING_TIMEOUT")
with db() as gate:
    assert gate.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",(gate_key,)).fetchone()[0]
print("PASS: single maintenance scanner across processes, automatic session release, deferred work recovered")

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
    # Same deferral rule applies to the original standalone recovery. Keep
    # the intentionally locked row held while checking the next quiet pass.
    tick()
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

# Native mixed batch recovery requires an explicit, complete target snapshot.
# All fixtures below are disposable and no publisher runs.
def mixed(states,owner="rust",auto=False):
    t=task(owner=owner); b=uuid4(); ids=[]
    targets=[{"platform":p,"account_id":None,"key":p+":default"} for p in ("bilibili","douyin")]
    with db() as c:
        c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets,request_json,updated_at) VALUES(%s,%s,'active',%s::jsonb,%s::jsonb,now()-interval '72 hours')",
            (b,t,json.dumps(targets),json.dumps({"runtime":"rust-mixed-v1","auto_parent":{"workflow_id":str(uuid4())} if auto else None})))
        c.execute("UPDATE tasks SET active_publish_batch_id=%s WHERE id=%s",(b,t))
        for p,state in zip(("bilibili","douyin"),states):
            j=uuid4(); ids.append(j)
            response={"fixture":"keep","progress":17}
            external="fixture-"+p if state=="published" else None
            if state=="unverified":
                state="submitted";response.update(runtime="rust",receipt_verified=False)
            c.execute("INSERT INTO publish_jobs(id,task_id,batch_id,platform,state,operation_key,external_id,response_json,upload_progress,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,17,now()-interval '72 hours')",
                (j,t,b,p,state,"publish-job:"+str(j),external,json.dumps(response)))
    return t,b,ids
def batch_state(b):
    with db() as c:return c.execute("SELECT state,finished_at,cleanup_enqueued_at FROM publish_batches WHERE id=%s",(b,)).fetchone()
def events(b):
    with db() as c:return c.execute("SELECT event_type,status,available_at>=now()+interval '23 hours' FROM outbox_events WHERE aggregate_type='publish_batch' AND aggregate_id=%s ORDER BY event_type",(str(b),)).fetchall()

success=mixed(["published","published"])
partial=mixed(["published","submitting"])
failure_batch=mixed(["failed","unknown"])
unverified=mixed(["published","unverified"])
held=mixed(["published","submitting"])
pending=mixed(["published","submitting"])
missing=mixed(["published"])
duplicate=mixed(["published","submitting"])
automatic=mixed(["published","submitting"],auto=True)
historical=mixed(["published","submitting"],owner="python")
foreign=mixed(["published","submitting"])
fresh_batch=mixed(["published","submitting"])
uncertain_receipt=mixed(["published","submitting"])
multiple=mixed(["published","submitting"])
with db() as c:
    c.execute("UPDATE publish_jobs SET lease_owner='fixture',lease_until=now()+interval '1 hour' WHERE id=%s",(held[2][1],))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'pending','{}')",(uuid4(),"publish-job:"+str(pending[2][1])))
    c.execute("UPDATE publish_jobs SET platform='bilibili' WHERE id=%s",(duplicate[2][1],))
    c.execute("UPDATE publish_jobs SET task_id=%s WHERE id=%s",(python,foreign[2][1]))
    c.execute("UPDATE publish_batches SET updated_at=now() WHERE id=%s",(fresh_batch[1],))
    c.execute("UPDATE publish_jobs SET external_id='uncertain-receipt' WHERE id=%s",(uncertain_receipt[2][1],))
    c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets) VALUES(%s,%s,'failed','[]')",(uuid4(),multiple[0]))
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(lambda _:tick(),range(2)))
# NOWAIT table locks deliberately defer contested rows. The next normal
# maintenance pass must converge once the competing transaction is gone.
tick()
assert task_state(success[0])==("published",None),(success,task_state(success[0]),batch_state(success[1]))
assert batch_state(success[1])[0]=="succeeded"
assert events(success[1])==[("publish.cleanup","native_pending",True),("workflow.publish.finished","native_pending",False)]
for item in (partial,unverified):
    assert task_state(item[0])==("publishing","PUBLISH_PARTIAL_FAILURE"),(item,task_state(item[0]))
    assert batch_state(item[1])[0]=="partial_failed"
    assert events(item[1])==[("workflow.publish.finished","native_pending",False)]
    assert receipt_state(item[2][0])==("published",{"fixture":"keep","progress":17},"fixture-bilibili")
    assert start(item[0])==409
state,body,receipt=receipt_state(partial[2][1])
assert state=="unknown" and body["fixture"]=="keep" and body["progress"]==17 and body["verify_platform_before_retry"] is True and receipt is None
assert receipt_state(unverified[2][1])[0]=="submitted"
assert task_state(failure_batch[0])==("failed","PUBLISH_FAILED")
assert batch_state(failure_batch[1])[0]=="failed"
assert receipt_state(failure_batch[2][1])==("unknown",{"fixture":"keep","progress":17},None)
for item in (held,pending,missing,duplicate,automatic,historical,foreign,fresh_batch,uncertain_receipt,multiple):
    assert task_state(item[0])==("publishing",None),(item,task_state(item[0]))
    assert batch_state(item[1])[0]=="active" and not events(item[1])
# Force another scan of an already reconciled partial batch: no extra event
# or changes to the finished_at timestamp should occur.
before=batch_state(partial[1])
with db() as c:c.execute("UPDATE tasks SET updated_at=now()-interval '72 hours' WHERE id=%s",(partial[0],))
tick()
assert batch_state(partial[1])==before and len(events(partial[1]))==1
with db() as c:
    assert c.execute("SELECT count(*) FROM outbox_events WHERE event_type='publish.dispatch'").fetchone()[0]==0
print("PASS: stale native mixed batch convergence, verified targets untouched, orphan quarantine, unverified receipt protection, no replay, conservative deferral, delayed cleanup and event idempotency")

# Automatic batch waiting is not an executing external publisher. The
# original immutable ticket, operation identity and generation must match.
def auto_batch(states, *, parent_state="running", waiter_state="awaiting", owner="rust", fault=None):
    t,b,ids=mixed(states,owner=owner)
    flow=uuid4(); stage=uuid4(); gen=3
    parent={"workflow_id":str(flow),"job_id":str(stage),"generation":gen}
    with db() as c:
        c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,updated_at) VALUES(%s,%s,%s,%s,'publish',now()-interval '72 hours')",
            (flow,t,gen,parent_state))
        c.execute("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,external_ticket,updated_at) VALUES(%s,%s,%s,'publish',%s,%s,%s,%s,now()-interval '72 hours')",
            (stage,flow,t,gen,f"vr2:{flow}:publish:{gen}",waiter_state,"native-batch:"+str(b)))
        c.execute("UPDATE publish_batches SET request_json=jsonb_set(request_json,'{auto_parent}',%s::jsonb) WHERE id=%s",(json.dumps(parent),b))
        if fault=="ticket":
            c.execute("UPDATE operation_inbox SET external_ticket=%s WHERE id=%s",("native-batch:"+str(uuid4()),stage))
        elif fault=="generation":
            c.execute("UPDATE publish_batches SET request_json=jsonb_set(request_json,'{auto_parent,generation}','4'::jsonb) WHERE id=%s",(b,))
        elif fault=="lease":
            c.execute("UPDATE operation_inbox SET lease_token=%s,lease_until=now()+interval '1 hour' WHERE id=%s",(uuid4(),stage))
        elif fault=="stale_token":
            c.execute("UPDATE operation_inbox SET lease_token=%s WHERE id=%s",(uuid4(),stage))
        elif fault=="fresh":
            c.execute("UPDATE operation_inbox SET updated_at=now() WHERE id=%s",(stage,))
        elif fault=="fresh_flow":
            c.execute("UPDATE pipeline_runs SET updated_at=now() WHERE id=%s",(flow,))
        elif fault=="other_operation":
            c.execute("INSERT INTO operation_inbox(id,operation_key,status,task_id,request_json) VALUES(%s,%s,'processing',%s,'{}')",(uuid4(),"fixture-other:"+str(uuid4()),t))
        elif fault=="other_workflow":
            c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES(%s,%s,4,'running','publish')",(uuid4(),t))
        elif fault=="delivery":
            c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'pending','{}')",(uuid4(),"publish-job:"+str(ids[1])))
        elif fault=="wrong_key":
            c.execute("UPDATE operation_inbox SET operation_key=%s WHERE id=%s",("wrong:"+str(stage),stage))
        elif fault=="result":
            c.execute("UPDATE operation_inbox SET result_json='{\"unexpected\":true}' WHERE id=%s",(stage,))
    return t,b,ids,flow,stage

def parent_state_of(item):
    with db() as c:
        return c.execute("SELECT w.state,i.status,i.external_ticket,i.result_json,i.finished_at FROM pipeline_runs w JOIN operation_inbox i ON i.workflow_id=w.id WHERE w.id=%s AND i.id=%s",(item[3],item[4])).fetchone()

auto_success=auto_batch(["published","published"])
auto_partial=auto_batch(["published","submitting"])
auto_failure=auto_batch(["failed","unknown"])
auto_unverified=auto_batch(["published","unverified"])
auto_blocked=[auto_batch(["published","submitting"],fault=f) for f in
    ("ticket","generation","lease","stale_token","fresh","fresh_flow","other_operation","other_workflow","delivery","wrong_key","result")]
auto_blocked.extend([auto_batch(["published","submitting"],parent_state="paused"),
                     auto_batch(["published","submitting"],waiter_state="running"),
                     auto_batch(["published","submitting"],owner="python")])
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(lambda _:tick(),range(2)))
tick()
for item,expected_batch,expected_task,expected_parent in (
    (auto_success,"succeeded",("published",None),"succeeded"),
    (auto_partial,"partial_failed",("publishing","PUBLISH_PARTIAL_FAILURE"),"unknown"),
    (auto_failure,"failed",("failed","PUBLISH_FAILED"),"unknown"),
    (auto_unverified,"partial_failed",("publishing","PUBLISH_PARTIAL_FAILURE"),"unknown"),
):
    t,b,ids,flow,stage=item
    assert task_state(t)==expected_task,(item,task_state(t))
    assert batch_state(b)[0]==expected_batch
    p=parent_state_of(item)
    assert p[:4]==(expected_parent,expected_parent,"native-batch:"+str(b),{"batch_id":str(b),"confirmed":expected_parent=="succeeded","batch_state":expected_batch}),p
    assert p[4] is not None
    assert len(events(b))==(2 if expected_parent=="succeeded" else 1)
    with db() as c:
        assert c.execute("SELECT count(*) FROM workflow_events WHERE workflow_id=%s AND kind=%s",(flow,"workflow.succeeded" if expected_parent=="succeeded" else "job.unknown")).fetchone()[0]==1
    assert start(t)==409
assert receipt_state(auto_partial[2][1])[0]=="unknown"
assert receipt_state(auto_unverified[2][1])[0]=="submitted"
for item in auto_blocked:
    assert task_state(item[0])==("publishing",None),(item,task_state(item[0]))
    assert batch_state(item[1])[0]=="active" and not events(item[1])
    assert receipt_state(item[2][1])[0]=="submitting"
snap=parent_state_of(auto_partial)
with db() as c:
    c.execute("UPDATE tasks SET updated_at=now()-interval '72 hours' WHERE id=%s",(auto_partial[0],))
tick()
assert parent_state_of(auto_partial)==snap
assert len(events(auto_partial[1]))==1
with db() as c:
    assert c.execute("SELECT count(*) FROM outbox_events WHERE event_type='publish.dispatch'").fetchone()[0]==0
print("PASS: exact automatic batch waiter identity, parent success/unknown settlement, active and ambiguous waiter deferral, idempotency and zero external replay")

# Terminal batches whose exact automatic parent is still waiting. The
# repair must never change the existing business receipt or retention date.
def terminal_batch(states, recorded, *, fault=None, notification="missing"):
    item=auto_batch(states)
    t,b,ids,flow,stage=item
    status,code={"succeeded":("published",None),"partial_failed":("publishing","PUBLISH_PARTIAL_FAILURE"),"failed":("failed","PUBLISH_FAILED")}[recorded]
    with db() as c:
        c.execute("UPDATE tasks SET status=%s::task_status,error_code=%s,updated_at=now()-interval '72 hours' WHERE id=%s",(status,code,t))
        c.execute("UPDATE publish_batches SET state=%s,finished_at=now()-interval '73 hours',updated_at=now()-interval '72 hours' WHERE id=%s",(recorded,b))
        c.execute("UPDATE publish_jobs SET finished_at=now()-interval '74 hours' WHERE batch_id=%s",(b,))
        if notification!="missing":
            event=uuid4(); key=f"workflow-publish-finished:{b}:{recorded}"
            args={"args":[str(b),recorded,str(t)]}
            if notification=="wrong_envelope":args["args"][2]=str(uuid4())
            c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status) VALUES(%s,'workflow.publish.finished','publish_batch',%s,'rust.publish.batch_finished',%s::jsonb,%s,%s)",
                (event,str(b),json.dumps(args),key,"native_completed" if notification=="completed" else "native_pending"))
            if notification=="completed":
                c.execute("INSERT INTO operation_inbox(id,operation_key,status,runtime_engine,request_json,result_json) VALUES(%s,%s,'completed','rust-publish-batch','{}','{}')",(uuid4(),key))
            if notification=="leased":
                c.execute("UPDATE outbox_events SET lease_owner='fixture',lease_until=now()+interval '1 hour' WHERE id=%s",(event,))
        if notification=="cleanup":
            c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status) VALUES(%s,'publish.cleanup','publish_batch',%s,'rust.publish.cleanup',%s::jsonb,%s,'native_pending')",
                (uuid4(),str(b),json.dumps({"args":[str(t),str(b)]}),f"publish-cleanup:{b}"))
        if fault=="missing_finish":c.execute("UPDATE publish_jobs SET finished_at=NULL WHERE id=%s",(ids[1],))
        elif fault=="late_finish":c.execute("UPDATE publish_jobs SET finished_at=now()-interval '71 hours' WHERE id=%s",(ids[1],))
        elif fault=="no_receipt":c.execute("UPDATE publish_jobs SET external_id=NULL WHERE id=%s",(ids[0],))
        elif fault=="newer":c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES(%s,%s,4,'succeeded','publish')",(uuid4(),t))
        elif fault=="canceled":c.execute("UPDATE tasks SET status='canceled' WHERE id=%s",(t,))
        elif fault=="different_status":c.execute("UPDATE tasks SET status='failed',error_code='OTHER' WHERE id=%s",(t,))
        elif fault=="other_batch":c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets) VALUES(%s,%s,'failed','[]')",(uuid4(),t))
        elif fault=="other_event":
            c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status) VALUES(%s,'publish.dispatch','task',%s,'social_publisher.process_job','{}',%s,'native_pending')",(uuid4(),str(t),"fixture:"+str(uuid4())))
    return item

def business_snapshot(item):
    t,b,ids,flow,stage=item
    with db() as c:
        return (c.execute("SELECT to_jsonb(t) FROM tasks t WHERE id=%s",(t,)).fetchone(),
                c.execute("SELECT to_jsonb(b) FROM publish_batches b WHERE id=%s",(b,)).fetchone(),
                c.execute("SELECT to_jsonb(j) FROM publish_jobs j WHERE batch_id=%s ORDER BY id",(b,)).fetchall(),
                c.execute("SELECT to_jsonb(e) FROM outbox_events e WHERE aggregate_type='publish_batch' AND aggregate_id=%s ORDER BY id",(str(b),)).fetchall())

terminal_ok=[
    (terminal_batch(["published","published"],"succeeded"),"succeeded"),
    (terminal_batch(["published","published"],"succeeded",notification="cleanup"),"succeeded"),
    (terminal_batch(["published","unknown"],"partial_failed",notification="completed"),"unknown"),
    (terminal_batch(["failed","unknown"],"failed",notification="pending"),"unknown"),
    (terminal_batch(["published","unverified"],"partial_failed"),"unknown"),
]
terminal_blocked=[terminal_batch(["published","published"],"succeeded",fault=f)
    for f in ("missing_finish","late_finish","no_receipt","newer","canceled","different_status","other_batch","other_event")]
terminal_blocked.extend([
    terminal_batch(["published","unknown"],"succeeded"),
    terminal_batch(["published","submitting"],"succeeded"),
    terminal_batch(["published","published"],"succeeded",notification="wrong_envelope"),
    terminal_batch(["published","published"],"succeeded",notification="leased"),
])
before_terminal={item[0]:business_snapshot(item) for item in [x[0] for x in terminal_ok]+terminal_blocked}
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(lambda _:tick(),range(2)))
tick()
for item,want in terminal_ok:
    actual=parent_state_of(item)
    assert actual[:2]==(want,want),(item,actual)
    assert actual[3]["batch_id"]==str(item[1]) and actual[3]["confirmed"]==(want=="succeeded")
    assert business_snapshot(item)==before_terminal[item[0]]
    with db() as c:
        assert c.execute("SELECT count(*) FROM workflow_events WHERE workflow_id=%s AND kind=%s",(item[3],"workflow.succeeded" if want=="succeeded" else "job.unknown")).fetchone()[0]==1
for item in terminal_blocked:
    assert parent_state_of(item)[:2]==("running","awaiting"),item
    assert business_snapshot(item)==before_terminal[item[0]]
snapshot={item[0]:parent_state_of(item) for item,_ in terminal_ok}
tick()
for item,_ in terminal_ok:
    assert parent_state_of(item)==snapshot[item[0]]
    assert business_snapshot(item)==before_terminal[item[0]]
print("PASS: terminal automatic batch exact waiter repair, immutable business snapshots, pending/completed/missing notifications, contradictory receipts and newer generations blocked, idempotent")

# Exercise the original native Bilibili cancellation path without running
# any external worker. The scheduler must preserve both prior JSON snapshots.
t=task(); j=job(t); flow=uuid4(); stage=uuid4()
prior={"fixture":"keep","error":"previous error","upload_progress":45}
inbox_prior={"partial_response":"keep"}
with db() as c:
    c.execute("UPDATE publish_jobs SET operation_key=%s,lease_owner='rust-v2',response_json=%s::jsonb,upload_progress=45 WHERE id=%s",("publish-job:"+str(j),json.dumps(prior),j))
    c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(%s,%s,1,'running','publish',%s::jsonb)",(flow,t,json.dumps({"bilibili_direct_v1":{"publish_job_id":str(j)}})))
    c.execute("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state) VALUES(%s,%s,%s,'publish',1,%s,'running')",(stage,flow,t,"fixture-stage:"+str(stage)))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json,result_json,lease_owner,runtime_engine) VALUES(%s,%s,'processing','{}',%s::jsonb,'rust-v2','python')",(uuid4(),"publish-job:"+str(j),json.dumps(inbox_prior)))
req=Request("http://127.0.0.1:18080/api/v2/workflows/"+str(flow)+"/cancel",method="POST",headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]})
with urlopen(req,timeout=10) as r:assert r.status==200
with db() as c:
    state,response,progress=c.execute("SELECT state::text,response_json,upload_progress FROM publish_jobs WHERE id=%s",(j,)).fetchone()
    assert state=="unknown" and response["previous_response"]==prior and response["fixture"]=="keep" and response["confirmed"] is False and progress==45
    status,result=c.execute("SELECT status,result_json FROM operation_inbox WHERE operation_key=%s",("publish-job:"+str(j),)).fetchone()
    assert status=="completed" and result["previous_result"]==inbox_prior and result["verify_platform_before_retry"] is True
print("PASS: native Bilibili unknown settlement preserves original response, progress and operation result")
