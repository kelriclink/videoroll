"""Original browser management on real Rust API + disposable PostgreSQL."""
import concurrent.futures, json, os, socket, subprocess, time, uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import psycopg

url = os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
token = os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
assert token.startswith("isolated-render-admin-")
exe = Path(__file__).resolve().parents[1] / "target/debug/videoroll-v2"
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base = f"http://127.0.0.1:{port}"
mgmt = "/api/v1/render-management"
def call(method, path, payload=None, auth=True, credential=None):
    headers = {"Content-Type":"application/json"}
    if auth: headers["x-videoroll-v2-internal-token"] = token
    if credential: headers["Authorization"] = f"Bearer {credential}"
    data = None if payload is None else json.dumps(payload).encode()
    try:
        r = urlopen(Request(base+path, data=data, headers=headers, method=method), timeout=5)
    except HTTPError as exc:
        r = exc
    with r:
        body = r.read()
        try: body = json.loads(body)
        except ValueError: pass
        return r.status, body
def ok(method, path, payload=None, **kwargs):
    code, body = call(method, path, payload, **kwargs)
    assert code in (200, 201), (method,path,code,body)
    return body
def invite():
    return ok("POST", mgmt+"/enrollments", {"label":"isolated native worker","ttl_minutes":30})
def enroll(inv, key):
    return call("POST","/render-workers/v1/enroll",{
        "worker_key":key,"name":"native fixture","platform":"linux","architecture":"x86_64",
        "version":"isolated","protocol_version":1,"render_spec_versions":[1],
        "capabilities":{"encoders":["h264"]},"resources":{"devices":[{"id":"gpu0","name":"fixture"}]},
        "labels":{},"max_concurrency":2,"enrollment_token":inv["token"]}, auth=False)
def db(): return psycopg.connect(url)
proc = subprocess.Popen([str(exe),"api"], env={**os.environ,
    "VIDEOROLL_V2_BIND":f"127.0.0.1:{port}","VIDEOROLL_V2_RENDER_MANAGEMENT_NATIVE":"true",
    "VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL":"false","VIDEOROLL_V2_SOURCE_SCANNER":"false",
    "VIDEOROLL_V2_REMOTE_RENDER":"false"})
try:
    for _ in range(100):
        try:
            if call("GET","/ready",auth=False)[0] == 200: break
        except (URLError,ConnectionError): time.sleep(.2)
        assert proc.poll() is None
    else: raise AssertionError("API did not become ready")
    assert call("POST",mgmt+"/enrollments",{"label":"unauthorized"},auth=False)[0] == 401
    revoked = invite()
    assert len(revoked["token"]) == 64
    assert ok("DELETE",mgmt+"/enrollments/"+revoked["id"])["status"] == "revoked"
    assert enroll(revoked,"revoked-fixture")[0] == 401
    valid = invite()
    code, enrolled = enroll(valid,"native-fixture")
    assert code == 200,(code,enrolled)
    worker = enrolled["worker"]["id"]
    credential = enrolled["credential"]
    assert enroll(valid,"replay-fixture")[0] == 401
    invitations = {row["id"]:row for row in ok("GET",mgmt+"/enrollments")}
    assert invitations[revoked["id"]]["status"] == "revoked"
    assert invitations[valid["id"]]["status"] == "consumed" and invitations[valid["id"]]["worker_id"] == worker
    with db() as c:
        assert c.execute("SELECT runtime_engine FROM render_workers WHERE id=%s",(worker,)).fetchone()[0] == "rust-v2"
        old=uuid.uuid4()
        c.execute("INSERT INTO render_workers(id,worker_key,name,platform) VALUES(%s,%s,'old fixture','linux')",(old,str(old)))
    rows = ok("GET",mgmt+"/workers")
    assert [r["id"] for r in rows] == [worker],rows
    assert rows[0]["device_control_supported"] is False
    assert call("PATCH",mgmt+"/workers/"+str(old),{"enabled":False})[0] == 404
    patch=ok("PATCH",mgmt+"/workers/"+worker,{"draining":True,"max_concurrency":3})
    assert patch["draining"] and patch["max_concurrency"] == 3
    assert call("PATCH",mgmt+"/workers/"+worker+"/devices/gpu0",{"enabled":False})[0] == 409
    assert ok("PATCH",mgmt+"/workers/"+worker,{"draining":False})["draining"] is False
    print("PASS: original management invitation -> native enrollment, revocation, replay rejection, native worker update and historical write protection",flush=True)
    # Real native projection: queued, active lease, expired lease, completed;
    # native jobs use operation_inbox, historical jobs use render_jobs.
    def native(state, lease):
        t,w,j,e=[uuid.uuid4() for _ in range(4)]
        with db() as c:
            c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','subtitle_ready','rust')",(t,))
            c.execute("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,current_stage) VALUES(%s,%s,1,'rust-v2','videoroll.rust.pipeline','running','render')",(w,t))
            c.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,workflow_id,stage,generation,status,external_ticket) VALUES(%s,%s,'rust-v2',%s,%s,'render',1,'awaiting',%s)",(j,"admin-fixture:"+str(j),t,w,str(e)))
            c.execute("INSERT INTO render_executions(id,job_id,task_id,workflow_id,runtime_engine,state,worker_id,lease_until,attempt) VALUES(%s,%s,%s,%s,'rust-v2',%s,%s,CASE WHEN %s::integer IS NULL THEN NULL ELSE now()+%s::integer * interval '1 second' END,%s)",(e,j,t,w,state,None if state=="queued" else worker,lease,lease,0 if state=="queued" else 1))
        return str(t),str(j),str(e)
    queued=native("queued",None)
    live=native("running",300)
    expired=native("running",-300)
    completed=native("completed",None)
    with db() as c:
        t,j,e=[uuid.uuid4() for _ in range(3)]
        c.execute("INSERT INTO tasks(id,source_type,source_license,status) VALUES(%s,'local','own','subtitle_ready')",(t,))
        c.execute("INSERT INTO render_jobs(id,task_id,status) VALUES(%s,%s,'failed')",(j,t))
        c.execute("INSERT INTO render_executions(id,render_job_id,worker_id,state) VALUES(%s,%s,%s,'failed')",(e,j,old))
    historical=str(e)
    rows=ok("GET",mgmt+"/executions")
    items={r["id"]:r for r in rows}
    assert set(items)=={queued[2],live[2],expired[2],completed[2],historical}
    assert items[queued[2]]["worker_id"] is None and items[queued[2]]["render_job_id"]==queued[1]
    assert items[completed[2]]["state"]=="succeeded"
    assert items[historical]["runtime_engine"]=="python"
    assert items[queued[2]]["cancel_supported"] and items[queued[2]]["requeue_supported"]
    assert items[expired[2]]["cancel_supported"] and items[expired[2]]["requeue_supported"]
    assert items[live[2]]["cancel_supported"] and not items[live[2]]["requeue_supported"]
    assert all(not items[x]["control_supported"] for x in (completed[2],historical))
    assert ok("GET",mgmt+"/executions/"+live[2])["task_id"]==live[0]
    assert [r["id"] for r in ok("GET",mgmt+"/executions?state=succeeded")]==[completed[2]]
    assert [r["id"] for r in ok("GET",mgmt+"/executions?task_id="+queued[0])]==[queued[2]]
    assert len(ok("GET",mgmt+"/executions?worker_id="+worker))==3
    node=ok("GET",mgmt+"/workers")[0]
    assert node["active_jobs"]==1 and node["available_slots"]==2,node
    assert call("POST",mgmt+"/executions/"+historical+"/requeue",{"reason":"fixture"})[0]==409
    with db() as c:
        assert c.execute("SELECT state FROM render_executions WHERE id=%s",(live[2],)).fetchone()[0]=="running"
        assert c.execute("SELECT state FROM render_executions WHERE id=%s",(historical,)).fetchone()[0]=="failed"
        assert c.execute("SELECT count(*) FROM outbox_events").fetchone()[0]==0
    # Retry preserves attempt budget; repeated requests do not enqueue a second job.
    requeue_path=mgmt+"/executions/"+expired[2]+"/requeue"
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:call("POST",requeue_path,{"reason":"expired fixture"}),range(3)))
    assert all(code==200 and data["state"]=="queued" for code,data in results),results
    with db() as c:
        assert c.execute("SELECT state,attempt,worker_id,fence_token,lease_until FROM render_executions WHERE id=%s",(expired[2],)).fetchone()==("queued",1,None,None,None)
        assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(expired[1],)).fetchone()[0]=="awaiting"
        assert c.execute("SELECT count(*) FROM workflow_events WHERE job_id=%s AND kind='render.admin_requeued'",(expired[1],)).fetchone()[0]==1
    # A canceled workflow is never silently requeued. Repeated cancellation is idempotent.
    cancel_path=mgmt+"/executions/"+queued[2]+"/cancel"
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:call("POST",cancel_path,{"reason":"cancel fixture"}),range(3)))
    assert all(code==200 and data["state"]=="canceled" for code,data in results),results
    assert call("POST",mgmt+"/executions/"+queued[2]+"/requeue",{})[0]==409
    exhausted=native("running",-300)
    uploaded=native("uploading",-300)
    foreign=native("queued",None)
    with db() as c:
        c.execute("UPDATE render_executions SET attempt=5 WHERE id=%s",(exhausted[2],))
        asset=uuid.uuid4()
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final',%s)",(asset,uploaded[0],"isolated/"+str(asset)))
        c.execute("UPDATE render_executions SET uploaded_asset_id=%s WHERE id=%s",(asset,uploaded[2]))
        c.execute("UPDATE operation_inbox SET external_ticket='different' WHERE id=%s",(foreign[1],))
        assert c.execute("SELECT status::text FROM tasks WHERE id=%s",(queued[0],)).fetchone()[0]=="canceled"
        assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(queued[1],)).fetchone()[0]=="canceled"
        assert c.execute("SELECT count(*) FROM workflow_events WHERE job_id=%s AND kind='workflow.canceled'",(queued[1],)).fetchone()[0]==1
        assert c.execute("SELECT state FROM pipeline_runs WHERE id=(SELECT workflow_id FROM operation_inbox WHERE id=%s)",(queued[1],)).fetchone()[0]=="canceled"
    for item in (uploaded,foreign):
        data=ok("GET",mgmt+"/executions/"+item[2]); assert not data["control_supported"]
        for action in ("cancel","requeue"):
            assert call("POST",mgmt+"/executions/"+item[2]+"/"+action,{})[0]==409
    data=ok("GET",mgmt+"/executions/"+exhausted[2]); assert data["cancel_supported"] and not data["requeue_supported"]
    assert call("POST",mgmt+"/executions/"+exhausted[2]+"/requeue",{})[0]==409
    assert call("POST",mgmt+"/executions/"+completed[2]+"/cancel",{})[0]==409
    assert call("POST",mgmt+"/executions/"+live[2]+"/requeue",{})[0]==409
    with db() as c:
        assert c.execute("SELECT count(*) FROM outbox_events").fetchone()[0]==0
    print("PASS: native queued cancel and expired requeue are atomic/idempotent; live requeue, uploaded output, exhausted budget, foreign ticket, completed and Python rows protected",flush=True)
    fence=uuid.uuid4().hex+uuid.uuid4().hex
    with db() as c:
        c.execute("UPDATE render_executions SET fence_token=%s WHERE id=%s",(fence,live[2]))
        before=c.execute("SELECT lease_until FROM render_executions WHERE id=%s",(live[2],)).fetchone()[0]
    path=mgmt+"/executions/"+live[2]+"/cancel"
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:call("POST",path,{"reason":"stop live worker"}),range(3)))
    assert all(code==200 and data["state"]=="canceled" for code,data in results),results
    endpoint="/render-workers/v1/executions/"+live[2]
    def worker_call(method,suffix,payload=None):
        return call(method,endpoint+suffix,payload,auth=False,credential=credential)
    code,poll=worker_call("GET","/cancel")
    assert code==200 and poll["cancel_requested"] and poll["reason"]=="stop live worker"
    assert worker_call("POST","/heartbeat",{"fence_token":fence,"progress":30,"metrics":{}})[0]==409
    assert worker_call("POST","/complete",{"fence_token":fence,"output":{}})[0]==409
    assert worker_call("POST","/fail",{"fence_token":"x"*64,"error":"wrong token","retryable":True})[0]==409
    with db() as c:
        assert c.execute("SELECT state,lease_until FROM render_executions WHERE id=%s",(live[2],)).fetchone()==("canceled",before)
        assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(live[1],)).fetchone()[0]=="canceled"
        assert c.execute("SELECT count(*) FROM workflow_events WHERE job_id=%s AND kind='workflow.canceled'",(live[1],)).fetchone()[0]==1
    assert ok("GET",mgmt+"/workers")[0]["active_jobs"]==1
    ok("PATCH",mgmt+"/workers/"+worker,{"max_concurrency":1})
    claim=ok("POST","/render-workers/v1/"+worker+"/claim",{"available_slots":1,"accepted_transfer_modes":["http"],"available_encoders":["h264"]},auth=False,credential=credential)
    assert claim["execution"] is None,claim
    for _ in range(2):
        code,body=worker_call("POST","/fail",{"fence_token":fence,"error":"worker stopped","retryable":True})
        assert code==200 and body["state"]=="canceled",(code,body)
    assert ok("GET",mgmt+"/workers")[0]["active_jobs"]==0
    offline=native("uploading",300)
    with db() as c:
        c.execute("UPDATE render_executions SET fence_token=%s WHERE id=%s",(uuid.uuid4().hex+uuid.uuid4().hex,offline[2]))
    assert ok("POST",mgmt+"/executions/"+offline[2]+"/cancel",{"reason":"offline worker"})["state"]=="canceled"
    assert ok("GET",mgmt+"/workers")[0]["active_jobs"]==1
    with db() as c:
        c.execute("UPDATE render_executions SET lease_until=now()-interval '1 second' WHERE id=%s",(offline[2],))
    subprocess.run([str(exe),"render-tick"],check=True,timeout=20)
    assert ok("GET",mgmt+"/workers")[0]["active_jobs"]==0
    with db() as c:
        for item in (live,offline):
            assert c.execute("SELECT state,lease_until FROM render_executions WHERE id=%s",(item[2],)).fetchone()==("canceled",None)
            assert c.execute("SELECT status FROM operation_inbox WHERE id=%s",(item[1],)).fetchone()[0]=="canceled"
            assert c.execute("SELECT count(*) FROM operation_inbox WHERE task_id=%s AND stage IN ('dubbing','publish')",(item[0],)).fetchone()[0]==0
    print("PASS: live cancellation, fence acknowledgment, stale heartbeat/completion rejected, reserved slot, idempotent stop and offline expiry",flush=True)
    revoked_worker=ok("POST",mgmt+"/workers/"+worker+"/revoke-credential",{})
    assert not revoked_worker["credential_active"] and not revoked_worker["enabled"]
    assert call("POST","/render-workers/v1/"+worker+"/heartbeat",{
        "status":"online","resources":{},"active_execution_ids":[]},auth=False,credential=credential)[0]==401
    print("PASS: revoked native node credential cannot heartbeat",flush=True)
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=5)
