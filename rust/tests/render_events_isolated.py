"""Original P2 administrative cancellation and native remote receipt recovery."""
import os, json, uuid, subprocess, concurrent.futures, time, hashlib
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
import psycopg

url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
exe=Path(os.environ.get("VIDEOROLL_TEST_SCHEMA_BINARY",str(Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"))).resolve(strict=True)
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
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES(%s,'local','own','published',%s,0,0)",(t,owner))
        c.execute("INSERT INTO render_workers(id,worker_key,name,platform,active_jobs,status) VALUES(%s,%s,'fixture','linux',1,'busy')",(w,str(w)))
        c.execute("INSERT INTO dubbing_jobs(id,task_id,status,output_json) VALUES(%s,%s,'rendering',%s)",(d,t,json.dumps({"render_job_id":str(uuid.uuid4() if wrong else r)})))
        c.execute("INSERT INTO render_jobs(id,task_id,status,request_json,progress,retry_count,lease_owner,lease_until) VALUES(%s,%s,'running',%s,0,0,%s,now()+interval '5 minutes')",(r,t,json.dumps({"dubbing_job_id":str(d)}),"render-worker:"+str(e)))
        c.execute("INSERT INTO render_executions(id,render_job_id,worker_id,state,fence_token,lease_until) VALUES(%s,%s,%s,'running',%s,now()+interval '5 minutes')",(e,r,w,str(e)))
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
            c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status,attempt_count) VALUES(%s,'workflow.dubbing.finished','dubbing_job',%s,'subtitle_service.push_dubbing_workflow_event','{}',%s,'pending',%s)",(uuid.uuid4(),str(d),k,int(attempted)))
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

def native(mode="burn_in",state="completed",wrong=False,canceled=False,ticket_wrong=False,uploaded=True):
    t,w,j,e,a=[uuid.uuid4() for _ in range(5)]
    key=f"v2/render/{t}/remote-{e}.mp4"
    if mode=="noop":key=f"v2/raw/{t}/original.mp4"
    path=root/key;path.parent.mkdir(parents=True,exist_ok=True)
    if uploaded: path.write_bytes(b"fixture")
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES(%s,'local','own',%s,'rust',0,0)",(t,"canceled" if canceled else "subtitle_ready"))
        c.execute("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,current_stage) VALUES(%s,%s,1,'rust-v2','videoroll.rust.pipeline','running','render')",(w,t))
        c.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,workflow_id,stage,generation,status,request_json,external_ticket) VALUES(%s,%s,'rust-v2',%s,%s,'render',1,'awaiting','{}',%s)",(j,"render-test:"+str(j),t,w,str(uuid.uuid4() if ticket_wrong else e)))
        if uploaded: c.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final',%s)",(a,t,key if not wrong else key+".different"))
        c.execute("INSERT INTO render_executions(id,job_id,task_id,workflow_id,runtime_engine,state,mode,uploaded_asset_id,render_spec,error_message) VALUES(%s,%s,%s,%s,'rust-v2',%s,%s,%s,%s,%s)",(e,j,t,w,state,mode,None if mode=="noop" or not uploaded else a,json.dumps({"request":{"input_key":key}}),"simulated renderer failure" if state=="failed" else None))
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
            c.execute("UPDATE render_executions SET uploaded_asset_id=NULL,attempt=%s,worker_id=%s,fence_token=%s,lease_until=CASE WHEN %s::int IS NULL THEN NULL ELSE now()+%s::int*interval '1 second' END WHERE id=%s",(attempts,worker,uuid.uuid4().hex+uuid.uuid4().hex,lease_seconds,lease_seconds,item[3]))
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
    # Use the actual worker upload HTTP endpoint and actual ffmpeg media,
    # then simulate the process dying before the completion callback.
    source=root/"recovery-fixture.mp4"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64:d=1","-c:v","mpeg4","-y",str(source)],check=True,timeout=30)
    media=source.read_bytes()
    def upload_fixture():
        item=native(state="running",uploaded=False)
        t,w,j,e,key=item
        f=uuid.uuid4().hex+uuid.uuid4().hex
        with db() as c:
            c.execute("UPDATE render_executions SET worker_id=%s,fence_token=%s,attempt=5,lease_until=now()+interval '5 minutes' WHERE id=%s",(worker,f,e))
        boundary="vr-upload-"+uuid.uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="fence_token"\r\n\r\n{f}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode()+media+f'\r\n--{boundary}--\r\n'.encode())
        req=Request(base+f"/render-workers/v1/executions/{e}/output",data=body,headers={"Authorization":"Bearer "+token,"Content-Type":"multipart/form-data; boundary="+boundary})
        with urlopen(req,timeout=20) as response: a=json.load(response)
        assert a["sha256"]==hashlib.sha256(media).hexdigest() and a["size_bytes"]==len(media)
        return item,f,a["id"]
    good,gf,ga=upload_fixture()
    corrupt,cf,ca=upload_fixture()
    still_live,lf,la=upload_fixture()
    # Explicit failure after committed upload must not discard the evidence.
    req=Request(base+f"/render-workers/v1/executions/{good[3]}/fail",data=json.dumps({"fence_token":gf,"error":"lost upload reply","retryable":True}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
    try:
        with urlopen(req,timeout=10): raise AssertionError("committed upload was failed")
    except HTTPError as exc: assert exc.code==409
    with db() as c:
        for item in (good,corrupt):
            c.execute("UPDATE render_executions SET lease_until=now()-interval '1 minute' WHERE id=%s",(item[3],))
        # Isolate claim eligibility: leave a free slot and a committed upload
        # below the attempt ceiling, with unrelated retry fixtures paused.
        c.execute("UPDATE render_executions SET attempt=4 WHERE id=%s",(corrupt[3],))
        c.execute("UPDATE pipeline_runs SET state='paused' WHERE id=%s",(retry[1],))
        c.execute("UPDATE render_workers SET max_concurrency=3,last_seen_at=now() WHERE id=%s",(worker,))
    with urlopen(claim,timeout=10) as response:
        assert json.load(response)["execution"] is None
    damaged=bytearray(media);damaged[-1]^=1;(root/corrupt[4]).write_bytes(damaged)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _:tick(),range(3)))
    tick()
    with db() as c:
        t,w,j,e,key=good
        state,receipt,attempt,lease=c.execute("SELECT state,output_json,attempt,lease_until FROM render_executions WHERE id=%s",(e,)).fetchone()
        assert state=="completed" and attempt==5 and lease is None and receipt["asset_id"]==ga and receipt["storage_key"]==key
        assert c.execute("SELECT result_json FROM operation_inbox WHERE id=%s",(j,)).fetchone()[0]==receipt
        assert c.execute("SELECT count(*) FROM operation_inbox WHERE workflow_id=%s AND stage='dubbing'",(w,)).fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM workflow_events WHERE workflow_id=%s AND kind='job.succeeded'",(w,)).fetchone()[0]==1
        for item in (corrupt,still_live):
            assert c.execute("SELECT state FROM render_executions WHERE id=%s",(item[3],)).fetchone()[0]=="uploading"
            assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(item[2],)).fetchone()[0]=="awaiting"
    # A worker replay with the original fence after recovery remains idempotent.
    req=Request(base+f"/render-workers/v1/executions/{good[3]}/complete",data=json.dumps({"fence_token":gf,"output_asset_id":ga,"output":{}}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
    with urlopen(req,timeout=10) as response: assert response.status==200
    print("PASS: actual uploaded MP4 recovered after lost callback at attempt five; corrupt bytes and live lease preserved; replay idempotent",flush=True)
    # A committed output survives V2 workflow pause, including the fifth
    # attempt, without a second render. Resume waits until the lease drains.
    paused,pf,pa=upload_fixture()
    def api_post(path, payload=None, worker=False):
        headers={"Content-Type":"application/json"}
        if worker:headers["Authorization"]="Bearer "+token
        else:headers["x-videoroll-v2-internal-token"]=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
        req=Request(base+path,data=json.dumps(payload or {}).encode(),headers=headers)
        try:r=urlopen(req,timeout=10)
        except HTTPError as exc:r=exc
        with r:return r.status,json.load(r)
    assert api_post(f"/api/v2/workflows/{paused[1]}/pause")[0]==200
    assert api_post(f"/api/v2/workflows/{paused[1]}/resume")[0]==200
    tick()
    with db() as c:
        assert c.execute("SELECT state,attempt,uploaded_asset_id::text,lease_until>now() FROM render_executions WHERE id=%s",(paused[3],)).fetchone()==("paused",5,pa,True)
    assert api_post(f"/render-workers/v1/executions/{paused[3]}/complete",{"fence_token":pf,"output_asset_id":pa,"output":{}},True)[0]==409
    assert api_post(f"/render-workers/v1/executions/{paused[3]}/fail",{"fence_token":pf,"error":"paused","retryable":False},True)[0]==200
    tick();tick()
    with db() as c:
        assert c.execute("SELECT state,attempt,uploaded_asset_id::text,lease_until FROM render_executions WHERE id=%s",(paused[3],)).fetchone()==("completed",5,pa,None)
        assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(paused[2],)).fetchone()[0]=="succeeded"
        assert c.execute("SELECT count(*) FROM operation_inbox WHERE workflow_id=%s AND stage='dubbing'",(paused[1],)).fetchone()[0]==1
    assert api_post(f"/render-workers/v1/executions/{paused[3]}/complete",{"fence_token":pf,"output_asset_id":pa,"output":{}},True)[0]==200
    print("PASS: paused uploaded fifth-attempt output survives immediate resume, waits for acknowledgment, then settles exactly once",flush=True)

    # Reuse real uploaded outputs and their actual settled receipt. A terminal
    # fixture only accelerates time; no external publication is performed.
    def gc():
        subprocess.run([str(exe),"gc","--limit","100"],check=True,timeout=40)
    def terminal(item):
        t,w,j,e,key=item
        with db() as c:
            c.execute("UPDATE tasks SET status='published',updated_at=now()-interval '3 days' WHERE id=%s",(t,))
            c.execute("UPDATE pipeline_runs SET state='succeeded' WHERE id=%s",(w,))
            c.execute("UPDATE operation_inbox SET status='canceled',lease_token=NULL,lease_until=NULL,lease_owner=NULL WHERE workflow_id=%s AND id<>%s",(w,j))
    def due(item):
        with db() as c:
            c.execute("UPDATE storage_objects SET retain_until=now()-interval '1 minute' WHERE storage_key=%s",(item[4],))
    def ttl(days):
        with db() as c:
            c.execute("INSERT INTO app_settings(key,value_json) VALUES('storage.retention',%s::jsonb) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({"asset_ttl_days":days}),))
    def refs(item):
        with db() as c:
            return c.execute("SELECT uploaded_asset_id::text,output_json,metrics FROM render_executions WHERE id=%s",(item[3],)).fetchone()
    terminal(good)
    terminal(paused)
    good_before=refs(good)
    paused_before=refs(paused)
    with db() as c:
        c.execute("UPDATE operation_inbox SET result_json=result_json||'{\"wrong_fixture\":true}'::jsonb WHERE id=%s",(paused[2],))
    ttl(1)
    gc()
    ttl(0)
    for item in (good,paused): due(item)
    gc()
    assert refs(good)==good_before and (root/good[4]).is_file()
    # Enable retention again but simulate an unlink failure. The real FK,
    # original asset and exact receipt must all survive the savepoint rollback.
    ttl(1)
    good_path=root/good[4]
    good_path.unlink()
    good_path.mkdir()
    due(good);due(paused);gc()
    assert good_path.is_dir() and refs(good)==good_before
    with db() as c:
        assert c.execute("SELECT count(*) FROM assets WHERE id=%s",(ga,)).fetchone()[0]==1
    assert refs(paused)==paused_before and (root/paused[4]).is_file()
    # Restore the fixture bytes, then allow the exact terminal output to retire.
    good_path.rmdir()
    good_path.write_bytes(media)
    due(good);due(paused);gc()
    after=refs(good)
    assert after[0] is None and after[1]==good_before[1] and after[2]["resource_retired"] is True,after
    assert not good_path.exists()
    assert refs(paused)==paused_before and (root/paused[4]).is_file()
    assert refs(still_live)[0]==la and (root/still_live[4]).is_file()
    with db() as c:
        assert c.execute("SELECT count(*) FROM assets WHERE id=%s",(ga,)).fetchone()[0]==0
        assert c.execute("SELECT result_json FROM operation_inbox WHERE id=%s",(good[2],)).fetchone()[0]==good_before[1]
        assert c.execute("SELECT count(*) FROM workflow_events WHERE workflow_id=%s AND kind='job.succeeded'",(good[1],)).fetchone()[0]==1
    endpoint=f"/render-workers/v1/executions/{good[3]}/complete"
    assert api_post(endpoint,{"fence_token":gf,"output_asset_id":ga,"output":{}},True)[0]==200
    assert api_post(endpoint,{"fence_token":"bad","output_asset_id":ga,"output":{}},True)[0]==409
    assert api_post(endpoint,{"fence_token":gf,"output_asset_id":str(uuid.uuid4()),"output":{}},True)[0]==409
    gc()
    assert refs(good)==after and not good_path.exists()
    print("PASS: terminal render FK retirement under TTL, failed unlink rollback, mismatched receipt/live upload preservation, authenticated late replay without re-creation",flush=True)


finally:
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
