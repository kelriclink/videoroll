"""Bounded bulk ownership cutover on the actual production DDL.
Requires the disposable schema-rehearsal database, never real rows.
"""
import concurrent.futures
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import psycopg

uri=os.environ["DATABASE_URL"]
u=urlsplit(uri)
assert u.hostname=="127.0.0.1" and u.path=="/videoroll_test"
assert os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]=="isolated-schema-rehearsal-token-0123456789"
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name.startswith("v2-schema-rehearsal.")
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","LANG","LC_ALL","DATABASE_URL","VIDEOROLL_V2_STORAGE_ROOT","VIDEOROLL_V2_INTERNAL_TOKEN","INTERNAL_API_SECRET")}
env.update(VIDEOROLL_V2_STANDALONE="true",VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="true",
    VIDEOROLL_V2_SOURCE_SCANNER="false",VIDEOROLL_V2_REMOTE_RENDER="false",
    VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI="false",VIDEOROLL_V2_ENABLE_SAU_PUBLISH="false",
    VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH="false",VIDEOROLL_V2_COMPAT_WRITE="false")
def db():return psycopg.connect(uri)
def cmd(*args,env_override=None):
    return subprocess.run([str(exe),*args],env={**env,**(env_override or {})},capture_output=True,text=True,timeout=90)
def run(*args,ok=True,env_override=None):
    p=cmd(*args,env_override=env_override)
    assert (p.returncode==0)==ok,(args,p.returncode,p.stdout,p.stderr)
    return json.loads(p.stdout[p.stdout.index("{"):]) if ok else p.stderr
def plan(limit=500,after=None):
    return run("task-ownership-bulk","--limit",str(limit),*(["--after",after] if after else []))
def apply(p,ok=True,extra=()):
    return run("task-ownership-bulk","--limit",str(p["limit"]),*(["--after",p["after"]] if p["after"] else []),"--apply","--quiesced","--plan-token",p["plan_token"],*extra,ok=ok)
def insert(c,t,status):
    c.execute("INSERT INTO tasks(id,source_type,source_license,status,priority,retry_count) VALUES(%s,'local','own',%s,0,0)",(t,status))
def owner(t):
    with db() as c:return c.execute("SELECT runtime_owner,status::text FROM tasks WHERE id=%s",(t,)).fetchone()
# Isolated fixture only. Ensure previous rehearsal left no Python owner.
with db() as c:
    assert c.execute("SELECT count(*) FROM tasks WHERE runtime_owner='python'").fetchone()[0]==0
    initial=c.execute("SELECT count(*) FROM operation_inbox WHERE runtime_engine='rust-task-cutover'").fetchone()[0]
    original=c.execute("SELECT count(*) FROM pipeline_runs").fetchone()[0]
    # deterministic UUID ordering for strict pagination and stale page tests
    ids={k:str(uuid.UUID(int=i+1)) for i,k in enumerate(["published","failed","canceled","unknown","locked","resume","pending","published_no_receipt","active","review","collision"])}
    for k,t in ids.items():
        status={"published":"published","failed":"failed","canceled":"canceled","unknown":"failed","locked":"failed","resume":"downloaded","pending":"failed","published_no_receipt":"published","active":"failed","review":"ready_for_review","collision":"failed"}[k]
        insert(c,t,status)
    c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,bvid,meta_json) VALUES(%s,%s,'bilibili','published','fixture-BV-bulk','{}')",(uuid.uuid4(),ids["published"]))
    c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,meta_json) VALUES(%s,%s,'douyin','unknown','{}')",(uuid.uuid4(),ids["unknown"]))
    c.execute("UPDATE tasks SET lock_owner='legacy-fixture',lock_until=now()-interval '1 day' WHERE id=%s",(ids["locked"],))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'pending',%s)",(uuid.uuid4(),"youtube-download:"+ids["pending"],json.dumps({"task_id":ids["pending"]})))
    c.execute("INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,external_run_id) VALUES(%s,%s,'hatchet','VideoPipelineV1','launch_failed','fixture')",(uuid.uuid4(),ids["active"]))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'completed','{}')",(uuid.uuid4(),"task-ownership-cutover:"+ids["collision"]))
    # Scale fixture to actual 5,083 historical tasks. Only synthetic rows.
    c.execute("""INSERT INTO tasks(id,source_type,source_license,status,priority,retry_count)
         SELECT gen_random_uuid(),'local','own','canceled',0,0 FROM generate_series(1,5072)""")
p=plan(11)
assert p["python_owned_total"]==5083 and len(p["items"])==11
rows={i["task_id"]:i for i in p["items"]}
assert p["counts"]=={"safe_terminal":3,"requires_reconciliation":6,"resume_candidate":1,"manual_review":1},p
assert rows[ids["resume"]]["resume_candidate"]=="subtitle"
for name,reason in [("unknown","publication_unsettled"),("locked","task_lease_present"),("pending","pending_download_requires_explicit_resume"),("published_no_receipt","published_without_confirmed_receipt"),("active","active_workflow"),("collision","adoption_receipt_already_exists")]:
    assert reason in rows[ids[name]]["blockers"],rows[ids[name]]
assert plan(11)==p
assert all(owner(t)[0]=="python" for t in ids.values())
run("task-ownership-bulk","--limit","501",ok=False)
run("task-ownership-bulk","--apply","--limit","11","--plan-token",p["plan_token"],ok=False)
run("task-ownership-bulk","--apply","--quiesced","--limit","11",ok=False)
run("task-ownership-bulk","--apply","--quiesced","--limit","11","--plan-token",p["plan_token"],env_override={"VIDEOROLL_V2_STANDALONE":"false"},ok=False)
with db() as c:
    # Plan drift after read-only review; no writes from rejected apply.
    c.execute("UPDATE tasks SET lock_owner='changed',lock_until=now() WHERE id=%s",(ids["failed"],))
apply(p,ok=False)
assert all(owner(t)[0]=="python" for t in ids.values())
with db() as c:c.execute("UPDATE tasks SET lock_owner=NULL,lock_until=NULL WHERE id=%s",(ids["failed"],))
p=plan(11)
with db() as c:
    c.execute("LOCK TABLE tasks IN ROW EXCLUSIVE MODE")
    apply(p,ok=False)
assert all(owner(t)[0]=="python" for t in ids.values())
# Mid-page failure injection: the first candidate may have been updated,
# but no ownership or receipt may survive the failed transaction.
with db() as c:
    c.execute("""CREATE FUNCTION fixture_reject_bulk() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.runtime_owner='rust' AND NEW.id='00000000-0000-0000-0000-000000000002' THEN
      RAISE EXCEPTION 'fixture abort'; END IF; RETURN NEW; END $$""")
    c.execute("CREATE TRIGGER fixture_reject_bulk BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION fixture_reject_bulk()")
apply(p,ok=False)
assert all(owner(t)[0]=="python" for t in ids.values())
with db() as c:
    assert c.execute("SELECT count(*) FROM operation_inbox WHERE runtime_engine='rust-task-cutover'").fetchone()[0]==initial
    c.execute("DROP TRIGGER fixture_reject_bulk ON tasks")
    c.execute("DROP FUNCTION fixture_reject_bulk()")
# Two simultaneous applies of same reviewed page: at most one commits.
args=["task-ownership-bulk","--limit","11","--apply","--quiesced","--plan-token",p["plan_token"]]
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results=list(pool.map(lambda _:cmd(*args),range(2)))
assert sorted(x.returncode==0 for x in results)==[False,True],[(x.stdout,x.stderr) for x in results]
a=json.loads(next(x.stdout for x in results if x.returncode==0))
assert a["adopted_count"]==3
assert set(a["adopted_task_ids"])=={ids[k] for k in ("published","failed","canceled")}
apply(p,ok=False)
assert run("task-ownership-adopt","--task",ids["published"],"--quiesced")["workflow_id"] is None
print("PASS: plan-only, bounded page, stale plan, explicit gates, lock contention, full page rollback and simultaneous/idempotent adoption",flush=True)
# Continue by reviewed cursor; no hidden worker scheduling.
start=time.monotonic();after=a["next_after"];n=3;pages=1
while after:
    p=plan(500,after)
    result=apply(p)
    n+=result["adopted_count"];pages+=1
    after=result["next_after"]
assert n==5075,(n,pages)
p=plan()
assert p["python_owned_total"]==8 and p["counts"].get("safe_terminal",0)==0,p
assert apply(p)["adopted_count"]==0
with db() as c:
    assert c.execute("SELECT count(*) FROM operation_inbox WHERE runtime_engine='rust-task-cutover'").fetchone()[0]==initial+n
    assert c.execute("SELECT count(*) FROM pipeline_runs").fetchone()[0]==original+1
    assert c.execute("SELECT count(*) FROM publish_jobs WHERE task_id IN (%s,%s)",(ids["published"],ids["unknown"])).fetchone()[0]==2
    assert c.execute("SELECT state::text FROM publish_jobs WHERE task_id=%s",(ids["unknown"],)).fetchone()[0]=="unknown"
    assert c.execute("SELECT lock_owner,lock_until IS NOT NULL FROM tasks WHERE id=%s",(ids["locked"],)).fetchone()==("legacy-fixture",True)
    assert c.execute("SELECT status FROM operation_inbox WHERE operation_key=%s",("youtube-download:"+ids["pending"],)).fetchone()[0]=="pending"
    assert c.execute("SELECT state FROM pipeline_runs WHERE task_id=%s",(ids["active"],)).fetchone()[0]=="launch_failed"
assert all(owner(ids[k])[0]=="python" for k in ids if k not in ("published","failed","canceled"))
print(f"PASS: 5083 synthetic tasks, {n} safe terminal adopted, 8 retained, {pages} pages, {time.monotonic()-start:.2f}s remaining page loop; no new workflows, no changed publication or lease evidence",flush=True)
