"""Global outbox cutover uses original tables and never contacts a broker."""
import os, json, uuid, subprocess
from pathlib import Path
import psycopg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from videoroll.apps.outbox.service import claim_outbox_events

url=os.environ["DATABASE_URL"]
assert url.endswith("/videoroll_test") and "127.0.0.1:" in url
exe=Path(os.environ.get("VIDEOROLL_TEST_SCHEMA_BINARY",str(Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"))).resolve(strict=True)
env={**os.environ,"VIDEOROLL_V2_NATIVE_OUTBOX_ONLY":"true","VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH":"false","VIDEOROLL_V2_COMPAT_WRITE":"false"}
def db():return psycopg.connect(url)
def run(*args,ok=True,overrides=None):
    r=subprocess.run([str(exe),*args],env={**env,**(overrides or {})},capture_output=True,text=True,timeout=25)
    assert (r.returncode==0)==ok,(args,r.returncode,r.stdout,r.stderr)
    if ok:return json.loads(r.stdout[r.stdout.index("{"):]) if "{" in r.stdout else None
def ev(handler,kind,aggregate_type,aggregate,key,args,status="pending",attempt=0,kwargs=None):
    id=uuid.uuid4()
    payload={"args":args,"queue":"subtitle-control"}
    if kwargs is not None:payload["kwargs"]=kwargs
    with db() as c:
        c.execute("""INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status,attempt_count)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(id,kind,aggregate_type,str(aggregate),handler,json.dumps(payload),key,status,attempt))
    return id
def task(owner="rust",old=False):
    id=uuid.uuid4()
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES(%s,'local','own','published',%s,0,0)",(id,owner))
        if old:c.execute("INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state) VALUES(%s,%s,'hatchet','test','running')",(uuid.uuid4(),id))
    return id
def batch(t,state="succeeded"):
    b=uuid.uuid4()
    with db() as c:c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets,request_json,outcomes_json,cleanup_delivery_version,finished_at) VALUES(%s,%s,%s,%s,'{}','{}',0,now()-interval '25 hours')",(b,t,state,json.dumps([{"platform":"bilibili"}])))
    return b
def notice(t,b,**kw):
    return ev("subtitle_service.push_publish_workflow_event","workflow.publish.finished","publish_batch",b,f"workflow-publish-finished:{b}:succeeded",[str(b),"succeeded",str(t)],**kw)
def get(id):
    with db() as c:return c.execute("SELECT status,task_name FROM outbox_events WHERE id=%s",(id,)).fetchone()
def sau(t,started=False,retry=False):
    j=uuid.uuid4();a=uuid.uuid4();key=f"publish-job:{j}"
    with db() as c:
        c.execute("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active,check_state) VALUES(%s,'douyin',%s,'fixture',true,'valid')",(a,str(a)))
        c.execute("""INSERT INTO publish_jobs(id,task_id,platform,account_id,state,operation_key,started_at,meta_json)
             VALUES(%s,%s,'douyin',%s,'submitting',%s,CASE WHEN %s THEN now() ELSE NULL END,%s)""",
             (j,t,a,key,started,json.dumps({"retry_of_job_id":str(uuid.uuid4())} if retry else {})))
    e=ev("social_publisher.process_job","publish.dispatch","publish_job",j,key,[str(j)])
    return e,key
assert run("outbox-audit")["delivery_ledger_ready"] is True
t=task();b=batch(t);safe=notice(t,b)
t2=task();b2=batch(t2);attempted=notice(t2,b2,attempt=1)
t3=task(owner="python");foreign=notice(t3,batch(t3))
t4=task(old=True);old_flow=notice(t4,batch(t4))
t5=task();b5=batch(t5);dup1=notice(t5,b5);dup2=notice(t5,b5)
t6=task();malformed=notice(t6,batch(t6),kwargs={"unexpected":True})
sau_event,sau_key=sau(task())
started,_=sau(task(),started=True)
retry,_=sau(task(),retry=True)
unknown=ev("plugin.unknown","custom","task",t,"unknown:"+str(t),[str(t)])
legacy=ev("subtitle_service.cancel_workflow_run","workflow.cancel","pipeline_run",uuid.uuid4(),"cancel:"+str(t),[],status="dispatched",attempt=1)
for handler in ("bilibili_publisher.process_job","bilibili_publisher.auto_dub_p2","subtitle_service.cleanup_task","subtitle_service.push_render_workflow_event","subtitle_service.push_dubbing_workflow_event","subtitle_service.after_render_publish"):
    ev(handler,"fixture","task",t,handler+":"+str(t),[])
report=run("outbox-audit")
assert len(report["legacy_handler_catalog"])==9 and report["unresolved_delivery_rows"]==18,report
run("outbox-audit","--require-ready",ok=False)
run("outbox-adopt",ok=False)
run("outbox-adopt","--quiesced",ok=False,overrides={"VIDEOROLL_V2_NATIVE_OUTBOX_ONLY":"false"})
run("outbox-tick",ok=False,overrides={"VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH":"true"})
# An active database writer prevents the entire transition (NOWAIT).
with db() as c:
    c.execute("LOCK TABLE outbox_events IN ROW EXCLUSIVE MODE")
    run("outbox-adopt","--quiesced",ok=False)
assert get(safe)[0]=="pending"
result=run("outbox-adopt","--quiesced")
assert result["adopted_batch_events"]==1 and result["adopted_sau_events"]==0,result
assert get(safe)==("native_pending","rust.publish.batch_finished")
assert get(sau_event)[0]=="pending"
result=run("outbox-adopt","--quiesced","--sau-ready")
assert result["adopted_batch_events"]==0 and result["adopted_sau_events"]==1,result
assert get(sau_event)==("native_pending","social_publisher.process_job")
with db() as c:
    assert c.execute("SELECT runtime_engine,status FROM operation_inbox WHERE operation_key=%s",(sau_key,)).fetchone()==("rust-sau-publish","pending")
for id in (attempted,foreign,old_flow,dup1,dup2,malformed,started,retry,unknown,legacy):
    assert get(id)[0] in ("pending","dispatched")
# Verify real Python dispatcher claim excludes the new Rust rows.
engine=create_engine(url.replace("postgresql://","postgresql+psycopg://",1))
with Session(engine) as session:
    claimed=claim_outbox_events(session,"isolated-old-dispatcher",100)
    assert safe not in [x.id for x in claimed] and sau_event not in [x.id for x in claimed]
    session.rollback()
result=run("outbox-tick")
assert result["batch_events"]==1 and result["render_receipts"]==0,result
assert get(safe)[0]=="native_completed"
assert run("outbox-tick")["batch_events"]==0
assert run("outbox-adopt","--quiesced","--sau-ready")["adopted_sau_events"]==0
report=run("outbox-audit")
assert report["unresolved_delivery_rows"]==16 and not report["delivery_ledger_ready"],report
assert any(g["disposition"]=="native_sau_queue" for g in report["groups"])
assert any(g["disposition"]=="native_acknowledged" for g in report["groups"])
print("PASS: global catalog, offline adoption guards and DB locks, original event UUIDs, SAU inbox creation, no broker replay, legacy dispatcher isolation, native unified tick, unresolved blockers preserved",flush=True)
