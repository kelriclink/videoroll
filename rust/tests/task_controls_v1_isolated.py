"""Original stop/resume controls, historical live jobs and native pause checkpoints."""
import json,os
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import uuid4
import psycopg

uri=os.environ["DATABASE_URL"]
assert urlsplit(uri).hostname=="127.0.0.1" and urlsplit(uri).path=="/videoroll_test"
def db():return psycopg.connect(uri)
def post(path):
    req=Request("http://127.0.0.1:18080/api/v1"+path,data=b"{}",method="POST",
      headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"],"content-type":"application/json"})
    try:r=urlopen(req,timeout=30)
    except HTTPError as e:r=e
    with r:return r.status,json.loads(r.read())
def task(status="created",stopped=None,owner="rust"):
    t=uuid4()
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,stopped_status,runtime_owner,error_code) VALUES(%s,'local','own',%s,%s,%s,'FIXTURE')",(t,status,stopped,owner))
    return t
def flow(t,stage,state="running",job_state="pending",generation=1):
    w,j=uuid4(),uuid4()
    with db() as c:
        c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES(%s,%s,%s,%s,%s)",(w,t,generation,state,stage))
        c.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,workflow_id,stage,generation,status) VALUES(%s,%s,'rust-v2',%s,%s,%s,%s,%s)",(j,"fixture:"+str(j),t,w,stage,generation,job_state))
    return w,j
def state(t):
    with db() as c:return c.execute("SELECT status::text,stopped_status::text FROM tasks WHERE id=%s",(t,)).fetchone()
def workflow(w):
    with db() as c:return c.execute("SELECT state,generation FROM pipeline_runs WHERE id=%s",(w,)).fetchone()
def snapshot(table):
    with db() as c:return c.execute("SELECT to_jsonb(x) FROM "+table+" x ORDER BY 1").fetchall()
def stop(t):return post("/tasks/"+str(t)+"/actions/stop")
def resume(t):return post("/tasks/"+str(t)+"/actions/resume")
bulk_stop="/tasks/actions/stop_all"
bulk_resume="/tasks/actions/resume_stopped"

normal=task("downloaded")
terminal=task("rendered");tw,tj=flow(terminal,"render","succeeded","succeeded")
failed_sub=task("failed")
failed_render=task("failed")
failed_native=task("failed");nw,nj=flow(failed_native,"subtitle")
failed_plain=task("failed")
already=task("canceled","ingested")
canceled=task("canceled")
published=task("published")
python=task("downloaded",owner="python")
unsafe=task("approved")
active_batch=task("downloaded")
multi=task("downloaded");flow(multi,"download");flow(multi,"subtitle",generation=2)
newer=task("downloaded");flow(newer,"download");flow(newer,"subtitle","succeeded","succeeded",2)
with db() as c:
    c.execute("INSERT INTO subtitle_jobs(id,task_id,status,request_json) VALUES(%s,%s,'running','{}')",(uuid4(),failed_sub))
    c.execute("INSERT INTO render_jobs(id,task_id,status,request_json) VALUES(%s,%s,'queued','{}')",(uuid4(),failed_render))
    c.execute("INSERT INTO publish_jobs(id,task_id,platform,state) VALUES(%s,%s,'bilibili','unknown')",(uuid4(),unsafe))
    c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets) VALUES(%s,%s,'active','[]')",(uuid4(),active_batch))
tables=("subtitle_jobs","render_jobs","publish_jobs","publish_batches","operation_inbox","outbox_events","assets")
original={t:snapshot(t) for t in tables}
assert stop(failed_sub)[0]==409 # individual semantics unchanged
assert stop(unsafe)[0]==409
assert stop(python)[0]==404
with db() as c:
    c.execute("LOCK TABLE publish_jobs IN ROW EXCLUSIVE MODE")
    assert post(bulk_stop)[0]==409
assert state(normal)==("downloaded",None)
# A real database error after some updates rolls back the entire batch.
with db() as c:
    c.execute("""CREATE FUNCTION fixture_stop_failure() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN IF NEW.status::text='canceled' AND NEW.error_code='FIXTURE_ABORT' THEN
       RAISE EXCEPTION 'fixture rollback'; END IF; RETURN NEW; END $$""")
    c.execute("CREATE TRIGGER fixture_stop_failure BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION fixture_stop_failure()")
    c.execute("UPDATE tasks SET error_code='FIXTURE_ABORT' WHERE id=%s",(max(normal,terminal,failed_sub,failed_render,failed_native),))
assert post(bulk_stop)[0]==500
assert state(normal)==("downloaded",None) and state(failed_native)==("failed",None)
assert workflow(nw)==("running",1)
with db() as c:
    c.execute("DROP TRIGGER fixture_stop_failure ON tasks")
    c.execute("DROP FUNCTION fixture_stop_failure()")
    c.execute("UPDATE tasks SET error_code='FIXTURE' WHERE error_code='FIXTURE_ABORT'")
code,body=post(bulk_stop)
assert code==200 and body=={"matched_count":9,"changed_count":5},(code,body)
for t,prev in [(normal,"downloaded"),(terminal,"rendered"),(failed_sub,"failed"),(failed_render,"failed"),(failed_native,"failed")]:
    assert state(t)==("canceled",prev),(t,state(t))
assert workflow(nw)==("paused",1) and workflow(tw)==("succeeded",1)
for t,prev in [(failed_plain,"failed"),(canceled,"canceled"),(published,"published"),(python,"downloaded"),(unsafe,"approved"),(active_batch,"downloaded"),(multi,"downloaded"),(newer,"downloaded")]:
    assert state(t)==(prev,None),(t,state(t))
assert {t:snapshot(t) for t in tables}==original
assert stop(normal)[0]==200 # idempotent
assert post(bulk_stop)[1]=={"matched_count":4,"changed_count":0}
# Native resumes same generation; quiescent terminal history stays terminal.
# Old live rows remain protected until explicitly reconciled.
code,body=post(bulk_resume)
assert code==200 and body=={"matched_count":6,"changed_count":4},(code,body)
assert state(terminal)==("rendered",None) and workflow(tw)==("succeeded",1)
assert state(normal)==("downloaded",None)
assert state(already)==("ingested",None)
assert state(failed_native)==("downloaded",None) and workflow(nw)==("running",1)
assert state(failed_sub)==state(failed_render)==("canceled","failed")
assert resume(failed_sub)[0]==409
assert {t:snapshot(t) for t in tables}==original
with db() as c:
    c.execute("UPDATE subtitle_jobs SET status='failed' WHERE task_id=%s",(failed_sub,))
    c.execute("UPDATE render_jobs SET status='failed' WHERE task_id=%s",(failed_render,))
assert post(bulk_resume)[1]=={"matched_count":2,"changed_count":2}
assert state(failed_sub)==state(failed_render)==("failed",None)
assert resume(canceled)[0]==409
assert stop(terminal)[0]==200 and resume(terminal)[0]==200
assert workflow(tw)==("succeeded",1)
# Concurrent duplicate controls must not create or restart generations.
race=task("downloaded");rw,rj=flow(race,"subtitle")
with ThreadPoolExecutor(max_workers=4) as pool:answers=list(pool.map(lambda _:stop(race),range(4)))
assert all(code in (200,409) for code,_ in answers),answers
assert state(race)==("canceled","downloaded") and workflow(rw)==("paused",1)
with ThreadPoolExecutor(max_workers=4) as pool:answers=list(pool.map(lambda _:resume(race),range(4)))
assert sum(code==200 for code,_ in answers)==1,answers
assert state(race)==("downloaded",None) and workflow(rw)==("running",1)
with db() as c:
    assert c.execute("SELECT count(*) FROM pipeline_runs WHERE task_id=%s",(race,)).fetchone()[0]==1
    assert c.execute("SELECT count(*) FROM operation_inbox WHERE task_id=%s",(race,)).fetchone()[0]==1
print("PASS: original bulk failed-live coverage, terminal workflow checkpoint restore, native same-generation recovery, unsafe publication/multiple generation fences, ownership, count semantics, transaction rollback and concurrent controls")
