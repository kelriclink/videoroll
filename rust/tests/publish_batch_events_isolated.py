"""Disposable database: native batch events and conservative asset GC."""
import os, subprocess, uuid, concurrent.futures, json
from pathlib import Path
import psycopg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from videoroll.apps.outbox.service import claim_outbox_events

url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
migration=Path(__file__).resolve().parents[1]/"migrations/20261124000038_native_publish_batch_events.sql"
def db(): return psycopg.connect(url)
def tick(command="publish-batch-tick"):
    subprocess.run([str(exe),command],check=True,timeout=30)
def event(batch,task,kind="cleanup",status="native_pending",attempt=0,legacy=False):
    key=f"publish-cleanup:{batch}" if kind=="cleanup" else f"workflow-publish-finished:{batch}:succeeded"
    name=("subtitle_service.cleanup_task" if kind=="cleanup" else "subtitle_service.push_publish_workflow_event") if legacy else ("rust.publish.cleanup" if kind=="cleanup" else "rust.publish.batch_finished")
    args=[str(task),str(batch)] if kind=="cleanup" else [str(batch),"succeeded",str(task)]
    with db() as c:
        c.execute("""INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status,attempt_count,available_at)
         VALUES(%s,%s,'publish_batch',%s,%s,%s,%s,%s,%s,now()-interval '1 second')""",
         (uuid.uuid4(),"publish.cleanup" if kind=="cleanup" else "workflow.publish.finished",str(batch),name,json.dumps({"args":args}),key,status,attempt))
    return key
def batch(task,platform="bilibili",state="succeeded",age=26):
    b=uuid.uuid4()
    with db() as c:
        c.execute("""INSERT INTO publish_batches(id,task_id,state,expected_targets,request_json,outcomes_json,cleanup_delivery_version,finished_at)
         VALUES(%s,%s,%s,%s,'{}','{}',0,now()-(%s*interval '1 hour'))""",(b,task,state,json.dumps([{"platform":platform}]),age))
    return b
def seed(owner="rust",final=True,age=26):
    task,flow,job=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES(%s,'local','own','published',%s,0,0)",(task,owner))
        c.execute("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,request_json) VALUES(%s,%s,1,'rust-v2','videoroll.rust.pipeline','succeeded',%s)",
                  (flow,task,json.dumps({"bilibili_direct_v1":{"publish_job_id":str(job)}})))
    b=batch(task,age=age)
    with db() as c:
        c.execute("INSERT INTO publish_jobs(id,task_id,batch_id,platform,state,external_id,meta_json) VALUES(%s,%s,%s,'bilibili','published','fixture-bvid','{}')",(job,task,b))
    keys={}
    for kind in (["video_raw","subtitle_srt","video_final","video_dubbed","dubbing_audio","publish_result"] if final else ["video_raw","subtitle_srt"]):
        key=f"v2/raw/{task}/{kind}"
        path=root/key;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b"fixture")
        with db() as c:
            c.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,%s,%s)",(uuid.uuid4(),task,kind,key))
            c.execute("INSERT INTO storage_objects(storage_key,size_bytes,state,retain_until) VALUES(%s,7,'live',now()-interval '1 hour')",(key,))
            c.execute("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(%s,'task',%s,%s),(%s,'workflow',%s,%s)",(key,task,kind,key,flow,kind))
        keys[kind]=key
    return task,flow,b,keys
def state(key):
    with db() as c:return c.execute("SELECT status,available_at>now(),task_name FROM outbox_events WHERE operation_key=%s",(key,)).fetchone()
def refs(key):
    with db() as c:return c.execute("SELECT count(*) FROM storage_object_refs WHERE storage_key=%s",(key,)).fetchone()[0]
def due(key):
    with db() as c:c.execute("UPDATE outbox_events SET available_at=now()-interval '1 second' WHERE operation_key=%s",(key,))

# A fresh batch is not eligible even if its event was delivered early.
task,flow,b,keys=seed(age=1)
key=event(b,task)
tick()
assert state(key)[:2]==("native_pending",True) and refs(keys["video_raw"])==2
# More recent independent target extends retention from its actual finish.
with db() as c:c.execute("UPDATE publish_batches SET finished_at=now()-interval '26 hours' WHERE id=%s",(b,))
peer=batch(task,"douyin",age=1)
due(key);tick()
assert state(key)[:2]==("native_pending",True) and refs(keys["video_raw"])==2
with db() as c:c.execute("UPDATE publish_batches SET finished_at=now()-interval '26 hours' WHERE id=%s",(peer,))
# Unknown publication, even outside the current successful batch, must pin.
unknown=uuid.uuid4()
with db() as c:c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,meta_json) VALUES(%s,%s,'douyin','unknown','{}')",(unknown,task))
due(key);tick()
assert state(key)[:2]==("native_pending",True) and refs(keys["video_raw"])==2
with db() as c:
    c.execute("UPDATE publish_jobs SET state='failed' WHERE id=%s",(unknown,))
    c.execute("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(%s,'playout',%s,'external')",(keys["subtitle_srt"],uuid.uuid4()))
due(key)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda _:tick(),range(3)))
assert state(key)[0]=="native_completed"
with db() as c:
    assert c.execute("SELECT count(*) FROM storage_workdirs WHERE task_id=%s AND state='retained'",(task,)).fetchone()[0]==4
assert refs(keys["video_raw"])==0 and refs(keys["subtitle_srt"])==1
for k in ("video_final","video_dubbed","dubbing_audio","publish_result"):
    assert refs(keys[k])==2 and (root/keys[k]).exists()
with db() as c:
    assert c.execute("SELECT cleanup_enqueued_at IS NOT NULL FROM publish_batches WHERE id=%s",(b,)).fetchone()[0]
    assert c.execute("SELECT count(*) FROM operation_inbox WHERE operation_key=%s AND runtime_engine='rust-publish-batch' AND status='completed'",(key,)).fetchone()[0]==1
    # Accelerate only the isolated eligible GC deadline.
    c.execute("UPDATE storage_objects SET retain_until=now()-interval '1 second' WHERE storage_key=%s",(keys["video_raw"],))
tick("gc")
assert not (root/keys["video_raw"]).exists()
assert (root/keys["subtitle_srt"]).exists() and (root/keys["video_final"]).exists()
print("PASS: 24h latest-target gate, unknown protection, three concurrent ticks, real GC unlink, shared ref and final retention",flush=True)

# No final file: latest raw must survive.
t,f,b,k=seed(final=False)
key=event(b,t)
tick()
assert state(key)[0]=="native_completed" and refs(k["video_raw"])==2 and refs(k["subtitle_srt"])==0
# A new current batch supersedes old cleanup.
t,f,b,k=seed()
key=event(b,t)
new=batch(t,age=1,state="active")
tick()
assert state(key)[0]=="native_completed" and refs(k["video_raw"])==2
print("PASS: only source video and superseded batch protected",flush=True)

# Only never-dispatched, never-attempted events belonging to Rust-owned tasks
# are adopted; retain original UUID, keys and delivery deadline.
t,f,b,k=seed()
safe=event(b,t,status="pending",legacy=True)
t2,f2,b2,k2=seed()
unsafe=event(b2,t2,status="dispatched",attempt=1,legacy=True)
t3,f3,b3,k3=seed(owner="python")
foreign=event(b3,t3,status="pending",legacy=True)
t4,f4,b4,k4=seed()
attempted=event(b4,t4,status="pending",attempt=1,legacy=True)
with db() as c:c.execute(migration.read_text())
assert state(safe)[::2]==("native_pending","rust.publish.cleanup")
assert state(unsafe)[::2]==("dispatched","subtitle_service.cleanup_task")
assert state(foreign)[::2]==("pending","subtitle_service.cleanup_task")
assert state(attempted)[::2]==("pending","subtitle_service.cleanup_task")
with Session(create_engine(url.replace("postgresql://","postgresql+psycopg://",1))) as session:
    assert safe not in [x.operation_key for x in claim_outbox_events(session,"isolated-legacy",100)]
tick()
assert state(safe)[0]=="native_completed"
print("PASS: safe legacy outbox adoption only; old dispatcher cannot claim native events",flush=True)
# Multiple historical outbox rows sharing a key cannot repeat the notification.
t,f,b,k=seed()
notice=event(b,t,kind="finished")
event(b,t,kind="finished")
tick();tick()
with db() as c:
    assert c.execute("SELECT count(*) FROM workflow_events WHERE kind='publish.batch.finished' AND details->>'operation_key'=%s",(notice,)).fetchone()[0]==1
    assert c.execute("SELECT count(*) FROM outbox_events WHERE operation_key=%s AND status='native_completed'",(notice,)).fetchone()[0]==2
print("PASS: duplicate historical event key acknowledged without repeated workflow notification",flush=True)

