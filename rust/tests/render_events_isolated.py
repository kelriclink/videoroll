"""Original P2 administrative cancellation and native remote receipt recovery."""
import os, json, uuid, subprocess, concurrent.futures, time, hashlib
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
import psycopg

url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
base="http://127.0.0.1:18194"
def db():return psycopg.connect(url)
def post(path,body,auth=True):
    headers={"Content-Type":"application/json"}
    if auth:headers["x-videoroll-v2-internal-token"]=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
    try:r=urlopen(Request(base+"/api/v1"+path,data=json.dumps(body).encode(),headers=headers),timeout=10)
    except HTTPError as e:r=e
    with r:return r.status,json.load(r)
def tick():
    subprocess.run([str(exe),"render-tick"],check=True,timeout=20)
def seed_legacy(owner="rust",wrong=False,oldflow=False):
    t,w,r,d,e=[uuid.uuid4() for _ in range(5)]
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','published',%s)",(t,owner))
        c.execute("INSERT INTO render_workers(id,worker_key,name,platform,active_jobs,status) VALUES(%s,%s,'fixture','linux',1,'busy')",(w,str(w)))
        c.execute("INSERT INTO dubbing_jobs(id,task_id,status,output_json) VALUES(%s,%s,'rendering',%s)",(d,t,json.dumps({"render_job_id":str(uuid.uuid4() if wrong else r)})))
        c.execute("INSERT INTO render_jobs(id,task_id,status,request_json,lease_owner,lease_until) VALUES(%s,%s,'running',%s,%s,now()+interval '5 minutes')",(r,t,json.dumps({"dubbing_job_id":str(d)}),"render-worker:"+str(e)))
        c.execute("INSERT INTO render_executions(id,render_job_id,worker_id,state,lease_until) VALUES(%s,%s,%s,'running',now()+interval '5 minutes')",(e,r,w))
        if oldflow:c.execute("INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state) VALUES(%s,%s,'hatchet','fixture','running')",(uuid.uuid4(),t))
    return t,w,r,d,e
def cancel(e,auth=True):return post(f"/render-management/executions/{e}/cancel",{"reason":"isolated cancellation"},auth)
def check_legacy(seed,expected):
    t,w,r,d,e=seed
    with db() as c:
        row=c.execute("SELECT state FROM render_executions WHERE id=%s",(e,)).fetchone()[0]
        assert row==expected,row
        assert c.execute("SELECT status FROM dubbing_jobs WHERE id=%s",(d,)).fetchone()[0]==("failed" if expected=="canceled" else "rendering")
        assert c.execute("SELECT status FROM render_jobs WHERE id=%s",(r,)).fetchone()[0]==expected
        assert c.execute("SELECT status::text FROM tasks WHERE id=%s",(t,)).fetchone()[0]=="published"
        assert c.execute("SELECT active_jobs FROM render_workers WHERE id=%s",(w,)).fetchone()[0]==(0 if expected=="canceled" else 1)
        key=f"workflow-dubbing-finished:{d}:failed"
        assert c.execute("SELECT count(*) FROM outbox_events WHERE operation_key=%s",(key,)).fetchone()[0]==0
        receipt=c.execute("SELECT runtime_engine,status,request_json,result_json FROM operation_inbox WHERE operation_key=%s",(key,)).fetchone()
        if expected=="canceled":
            assert receipt[:2]==("rust-render-events","completed") and receipt[2]["execution_id"]==str(e) and receipt[3]["status"]=="failed"
        else:assert receipt is None
proc=subprocess.Popen([str(exe),"api"],env={**os.environ,"VIDEOROLL_V2_BIND":"127.0.0.1:18194","VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL":"true"})
try:
    for _ in range(60):
        try:
            with urlopen(base+"/ready",timeout=1) as r:
                if r.status==200:break
        except URLError:time.sleep(.2)
    else:raise AssertionError("isolated API unavailable")
    seed=seed_legacy()
    assert cancel(seed[-1],False)[0]==401
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:cancel(seed[-1]),range(4)))
    assert all(code==200 for code,_ in results),results
    check_legacy(seed,"canceled")
    for item in (seed_legacy(owner="python"),seed_legacy(wrong=True),seed_legacy(oldflow=True)):
        assert cancel(item[-1])[0]==409
        check_legacy(item,"running")
    for attempted in (False,True):
        t,w,r,d,e=seed_legacy()
        k=f"workflow-dubbing-finished:{d}:failed"
        with db() as c:
            c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,operation_key,status,attempt_count) VALUES(%s,'workflow.dubbing.finished','dubbing_job',%s,'subtitle_service.push_dubbing_workflow_event',%s,'pending',%s)",(uuid.uuid4(),str(d),k,int(attempted)))
        assert cancel(e)[0]==(409 if attempted else 200)
        with db() as c:
            assert c.execute("SELECT status,task_name FROM outbox_events WHERE operation_key=%s",(k,)).fetchone()==(("pending","subtitle_service.push_dubbing_workflow_event") if attempted else ("native_completed","rust.render.dubbing_failed"))
            assert c.execute("SELECT status FROM dubbing_jobs WHERE id=%s",(d,)).fetchone()[0]==("rendering" if attempted else "failed")
            assert c.execute("SELECT count(*) FROM operation_inbox WHERE operation_key=%s",(k,)).fetchone()[0]==(0 if attempted else 1)
    assert post(f"/render-management/executions/{seed[-1]}/requeue",{"reason":"isolated requeue"})[0]==409
    print("PASS: original P2 cancel is atomic and idempotent without Celery; foreign binding/ownership/active Hatchet rejected",flush=True)
finally:
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)

def native(mode="burn_in",state="completed",wrong=False,canceled=False,ticket_wrong=False):
    t,w,j,e,a=[uuid.uuid4() for _ in range(5)]
    key=f"v2/render/{t}/remote-{e}.mp4"
    if mode=="noop":key=f"v2/raw/{t}/original.mp4"
    path=root/key;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b"fixture")
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own',%s,'rust')",(t,"canceled" if canceled else "subtitle_ready"))
        c.execute("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,current_stage) VALUES(%s,%s,1,'rust-v2','videoroll.rust.pipeline','running','render')",(w,t))
        c.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,workflow_id,stage,generation,status,external_ticket) VALUES(%s,%s,'rust-v2',%s,%s,'render',1,'awaiting',%s)",(j,"render-test:"+str(j),t,w,str(uuid.uuid4() if ticket_wrong else e)))
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final',%s)",(a,t,key if not wrong else key+".different"))
        c.execute("INSERT INTO render_executions(id,job_id,task_id,workflow_id,runtime_engine,state,mode,uploaded_asset_id,render_spec,error_message) VALUES(%s,%s,%s,%s,'rust-v2',%s,%s,%s,%s,%s)",(e,j,t,w,state,mode,None if mode=="noop" else a,json.dumps({"request":{"input_key":key}}),"simulated renderer failure" if state=="failed" else None))
    return t,w,j,e,key
ok=native()
noop=native("noop")
failed=native(state="failed")
bad=native(wrong=True)
cancelled=native(canceled=True)
mismatch=native(ticket_wrong=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda _:tick(),range(3)))
tick()
with db() as c:
    for t,w,j,e,key in (ok,noop):
        state,result=c.execute("SELECT status,result_json FROM operation_inbox WHERE id=%s",(j,)).fetchone()
        assert state=="succeeded" and result["remote_execution_id"]==str(e) and result["storage_key"]==key,(state,result)
        assert c.execute("SELECT count(*) FROM operation_inbox WHERE workflow_id=%s AND stage='dubbing' AND runtime_engine='rust-v2'",(w,)).fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM workflow_events WHERE workflow_id=%s AND kind='job.succeeded'",(w,)).fetchone()[0]==1
    t,w,j,e,key=failed
    assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(j,)).fetchone()[0]=="failed"
    assert c.execute("SELECT state FROM pipeline_runs WHERE id=%s",(w,)).fetchone()[0]=="failed"
    for t,w,j,e,key in (bad,cancelled,mismatch):
        assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(j,)).fetchone()[0]=="awaiting"
        assert c.execute("SELECT count(*) FROM operation_inbox WHERE workflow_id=%s AND stage='dubbing'",(w,)).fetchone()[0]==0
    assert c.execute("SELECT count(*) FROM outbox_events").fetchone()[0]==2
print("PASS: persisted render completion/failure recovery, noop pinned input, exact ticket/asset/ownership, three concurrent ticks, no duplicate next stage or Python events",flush=True)

# Real worker HTTP completion stores exactly the receipt consumed on replay.
t,w,j,e,key=native(state="running")
worker=uuid.uuid4()
token="a"*64
fence="b"*64
with db() as c:
    c.execute("INSERT INTO render_workers(id,worker_key,name,platform,runtime_engine,credential_hash) VALUES(%s,%s,'native-fixture','linux','rust-v2',%s)",(worker,str(worker),hashlib.sha256(token.encode()).hexdigest()))
    c.execute("UPDATE render_executions SET worker_id=%s,fence_token=%s,lease_until=now()+interval '5 minutes' WHERE id=%s",(worker,fence,e))
    asset=c.execute("SELECT uploaded_asset_id FROM render_executions WHERE id=%s",(e,)).fetchone()[0]
proc=subprocess.Popen([str(exe),"api"],env={**os.environ,"VIDEOROLL_V2_BIND":"127.0.0.1:18194"})
try:
    for _ in range(60):
        try:
            with urlopen(base+"/ready",timeout=1) as r:
                if r.status==200:break
        except URLError:time.sleep(.2)
    else:raise AssertionError("isolated API unavailable")
    def complete(f):
        req=Request(base+f"/render-workers/v1/executions/{e}/complete",data=json.dumps({"fence_token":f,"output_asset_id":str(asset),"output":{}}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
        try:r=urlopen(req,timeout=10)
        except HTTPError as exc:r=exc
        with r:return r.status,json.load(r)
    assert complete("bad")[0]==409
    code,body=complete(fence)
    assert code==200,(code,body)
    assert complete(fence)[0]==200
    with db() as c:
        state,receipt=c.execute("SELECT state,output_json FROM render_executions WHERE id=%s",(e,)).fetchone()
        assert state=="completed" and receipt["storage_key"]==key
        assert c.execute("SELECT result_json FROM operation_inbox WHERE id=%s",(j,)).fetchone()[0]==receipt
        assert c.execute("SELECT count(*) FROM operation_inbox WHERE workflow_id=%s AND stage='dubbing'",(w,)).fetchone()[0]==1
    print("PASS: HTTP worker completion, durable exact receipt, wrong fence rejected, duplicate callback idempotent",flush=True)
    # A sixth attempt is forbidden even before the recovery dispatcher ticks.
    expired=native(state="running")
    queued=native(state="queued")
    def set_attempt(item, attempts, lease_seconds, state=None):
        with db() as c:
            c.execute("UPDATE render_executions SET attempt=%s,worker_id=%s,fence_token=%s,lease_until=CASE WHEN %s::int IS NULL THEN NULL ELSE now()+%s::int*interval '1 second' END WHERE id=%s",(attempts,worker,uuid.uuid4().hex+uuid.uuid4().hex,lease_seconds,lease_seconds,item[3]))
            if state:
                c.execute("UPDATE pipeline_runs SET state=%s WHERE id=%s",(state,item[1]))
    set_attempt(expired,5,-60)
    set_attempt(queued,5,None)
    claim=Request(base+f"/render-workers/v1/{worker}/claim",data=json.dumps({"available_slots":1,"available_encoders":["h264"],"accepted_transfer_modes":["http"]}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
    with urlopen(claim,timeout=10) as response:
        assert json.load(response)["execution"] is None
    live=native(state="running")
    retry=native(state="uploading")
    paused=native(state="running")
    unrelated=native(state="running",ticket_wrong=True)
    set_attempt(live,5,300)
    set_attempt(retry,4,-60)
    set_attempt(paused,5,-60,"paused")
    set_attempt(unrelated,5,-60)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _:tick(),range(3)))
    tick()
    with db() as c:
        for t,w,j,e,key in (expired,queued):
            state,attempt,lease,finished=c.execute("SELECT state,attempt,lease_until,finished_at FROM render_executions WHERE id=%s",(e,)).fetchone()
            assert (state,attempt,lease)==("failed",5,None) and finished is not None
            assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(j,)).fetchone()[0]=="failed"
            assert c.execute("SELECT state FROM pipeline_runs WHERE id=%s",(w,)).fetchone()[0]=="failed"
            assert c.execute("SELECT count(*) FROM workflow_events WHERE workflow_id=%s AND kind='render.remote_failed'",(w,)).fetchone()[0]==1
        for item,state in ((live,"running"),(retry,"uploading"),(paused,"running"),(unrelated,"running")):
            assert c.execute("SELECT state FROM render_executions WHERE id=%s",(item[3],)).fetchone()[0]==state
            assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(item[2],)).fetchone()[0]=="awaiting"
    print("PASS: exhausted expired or queued render attempt budget, no sixth claim, exact failure receipt, live/retryable/paused/foreign ticket untouched",flush=True)
finally:
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
