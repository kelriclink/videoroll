#!/usr/bin/env python3
"""Rust SAU execution against disposable PostgreSQL and a no-network stub."""
import concurrent.futures
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from videoroll.apps.security.service_auth import service_token
from videoroll.apps.outbox.service import claim_outbox_events

url = os.environ["DATABASE_URL"]
assert url.endswith("/videoroll_test") and "127.0.0.1:" in url
token = service_token(SimpleNamespace(internal_api_secret=os.environ["INTERNAL_API_SECRET"], development_mode=False))
exe = Path(__file__).resolve().parents[1] / "target/debug/videoroll-sau"

def call(path, body=None):
    req = Request("http://127.0.0.1:18192" + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"X-Videoroll-Internal-Token": token, "Content-Type": "application/json"})
    try:
        response = urlopen(req, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)

def wait_for(fn, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("isolated SAU worker did not settle")

def stop(proc, hard=False):
    if proc.poll() is None:
        (proc.kill if hard else proc.terminate)()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

def row(job):
    with psycopg.connect(url) as conn:
        return conn.execute("""SELECT j.state::text,j.response_json,j.started_at,e.status,i.status,i.result_json
          FROM publish_jobs j JOIN outbox_events e ON e.operation_key=j.operation_key
          JOIN operation_inbox i ON i.operation_key=j.operation_key WHERE j.id=%s""", (job,)).fetchone()

def terminal(job, expected):
    def check():
        result = row(job)
        if result[0] in ("failed", "unknown", "submitted") and result[0] != expected:
            raise AssertionError(result)
        return result if result[0] == expected and result[3:5] == ("native_completed", "completed") else None
    return wait_for(check)

with tempfile.TemporaryDirectory(prefix="vr2-sau-worker-test-") as tmp:
    root = Path(tmp)
    (root / "cookies").mkdir(mode=0o700)
    store = root / "objects"
    store.mkdir()
    mock = root / "sau"
    mock.write_text("""#!/usr/bin/env python3
import sys,time
from pathlib import Path
p,mode,flag,name,*args=sys.argv[1:]
assert mode=='upload-video' and flag=='--account'
assert Path(args[args.index('--file')+1]).is_file()
assert args[-1]==('--headed' if p=='douyin' else '--headless')
assert ('--thumbnail' in args)==(p!='douyin')
with open('calls','a') as f:f.write(name+'\\n')
if name in ('hold','interrupted'):
    while not Path('release').exists():time.sleep(.05)
if name=='timeout':time.sleep(30)
if name=='nonzero':sys.exit(1)
""")
    mock.chmod(0o700)

    def spawn():
        env = os.environ.copy()
        env.update(VIDEOROLL_V2_SAU_BIND="127.0.0.1:18192",
                   VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK="true",
                   VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN="false",
                   VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH="true",
                   VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX="false",
                   SAU_RUNTIME_DIR=str(root), SAU_COOKIES_DIR=str(root / "cookies"),
                   SAU_EXECUTABLE=str(mock), STORAGE_ROOT=str(store), SAU_UPLOAD_TIMEOUT_SECONDS="5")
        proc = subprocess.Popen([str(exe)], env=env)
        def ready():
            assert proc.poll() is None
            try:
                return call("/health")[0] == 200
            except URLError:
                return False
        try:
            wait_for(ready)
        except BaseException:
            stop(proc)
            raise
        return proc

    def seed(platform, account_name, missing=False):
        account, task = uuid.uuid4(), uuid.uuid4()
        video, cover = f"{task}/video.mp4", f"{task}/cover.jpg"
        (store / video).parent.mkdir()
        if not missing:
            (store / video).write_bytes(b"fixture")
        (store / cover).write_bytes(b"fixture")
        with psycopg.connect(url) as conn:
            conn.execute("""INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active,check_state)
              VALUES(%s,%s,%s,%s,true,'valid')""",
              (account,platform,account_name,'dbplain:v1:{"cookies":[],"origins":[]}'))
            conn.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','approved','rust')", (task,))
            for kind,key in (("video_final",video),("cover_image",cover)):
                conn.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,%s,%s)", (uuid.uuid4(),task,kind,key))
        return {"task_id":str(task),"account_id":str(account),"platform":platform,
                "video":{"key":video},"cover":{"key":cover},"meta":{"title":"fixture"}}

    def submit(body):
        code,result = call("/sau/" + body["platform"] + "/publish", body)
        assert code == 200, (code,result)
        return result["job_id"]

    def calls():
        return (root / "calls").read_text().splitlines() if (root / "calls").exists() else []

    proc = spawn()
    try:
        held = seed("douyin","hold")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda _: submit(held), range(6)))
        assert len(set(ids)) == 1
        job = ids[0]
        wait_for(lambda: "hold" in calls())
        queued = submit(seed("xiaohongshu","queued"))
        assert row(queued)[3:5] == ("native_pending","pending")
        with Session(create_engine(url.replace("postgresql://","postgresql+psycopg://",1))) as session:
            assert claim_outbox_events(session,"isolated-dispatcher",100) == []
        (root / "release").touch()
        result = terminal(job,"submitted")
        assert result[1]["receipt_verified"] is False
        terminal(queued,"submitted")
        assert submit(held) == job and calls().count("hold") == 1
        print("PASS: native execution, concurrent idempotency, Python dispatcher exclusion",flush=True)

        for platform,n,status,missing in (("kuaishou","ok","submitted",False),
                ("douyin","missing","failed",True),("xiaohongshu","nonzero","unknown",False),
                ("kuaishou","timeout","unknown",False)):
            body = seed(platform,n,missing)
            job = submit(body)
            result = terminal(job,status)
            if missing:
                assert result[2] is None and n not in calls()
            if n == "timeout":
                assert result[1]["timed_out"] is True
            if status == "unknown":
                assert submit(body) == job
                assert call("/sau/"+platform+"/publish",{**body,"force_retry":True})[0] == 409
        print("PASS: three platforms, preflight failure, uncertain external results",flush=True)
        # CLI exit=0 is not independent platform evidence: a batch remains
        # unverified, with no legacy workflow or cleanup dispatch.
        batch_body=seed("kuaishou","batch")
        batch_id=uuid.uuid4()
        with psycopg.connect(url) as conn:
            conn.execute("INSERT INTO publish_batches(id,task_id,expected_targets) VALUES(%s,%s,%s)",
                (batch_id,batch_body["task_id"],json.dumps([{"platform":"kuaishou","account_id":batch_body["account_id"]}])))
        batch_body["batch_id"]=str(batch_id)
        batch_job=submit(batch_body)
        terminal(batch_job,"submitted")
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT state FROM publish_batches WHERE id=%s",(batch_id,)).fetchone()[0]=="failed"
            assert conn.execute("SELECT count(*) FROM outbox_events WHERE aggregate_id=%s",(str(batch_id),)).fetchone()[0]==0
        print("PASS: unverified batch does not become published or enqueue Python follow-ups",flush=True)
        intent=uuid.uuid4()
        with psycopg.connect(url) as conn:
            conn.execute("""INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,stage,generation,status,
                  sau_platform,sau_account_id,sau_video_key,sau_fingerprint,attempt_count,max_attempts,sau_job_id)
                  VALUES(%s,%s,'rust-v2',%s,'sau_publish',0,'accepted','kuaishou',%s,%s,%s,1,1,%s)""",
                (intent,"v2:sau:intent:"+str(intent),batch_body["task_id"],batch_body["account_id"],
                 batch_body["video"]["key"],"a"*64,batch_job))
        controller=exe.with_name("videoroll-v2")
        subprocess.run([str(controller),"social-tick"],check=True,timeout=15)
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT status FROM operation_inbox WHERE id=%s",(intent,)).fetchone()[0]=="unknown"
            conn.execute("UPDATE publish_jobs SET external_id='mock-confirmed' WHERE id=%s",(batch_job,))
        subprocess.run([str(controller),"social-tick"],check=True,timeout=15)
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT status,sau_external_id FROM operation_inbox WHERE id=%s",(intent,)).fetchone()==("submitted","mock-confirmed")
        print("PASS: coordinator converges to unknown without a receipt, then accepts separately confirmed receipt",flush=True)


        (root / "release").unlink()
        interrupted = seed("douyin","interrupted")
        job = submit(interrupted)
        wait_for(lambda: "interrupted" in calls())
        stop(proc,hard=True)
        with psycopg.connect(url) as conn:
            conn.execute("UPDATE publish_jobs SET lease_until=now()-interval '1 second' WHERE id=%s",(job,))
            conn.execute("UPDATE outbox_events SET lease_until=now()-interval '1 second' WHERE aggregate_id=%s",(job,))
            conn.execute("UPDATE operation_inbox SET lease_until=now()-interval '1 second' WHERE operation_key=%s",("publish-job:"+job,))
        proc = spawn()
        terminal(job,"unknown")
        assert submit(interrupted) == job and calls().count("interrupted") == 1
        print("PASS: interrupted worker recovery; never replay browser submission",flush=True)
    finally:
        stop(proc)
