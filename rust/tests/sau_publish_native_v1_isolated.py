#!/usr/bin/env python3
"""Native SAU HTTP intake; no browser, broker dispatch or real platform I/O."""
import concurrent.futures
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import psycopg
from videoroll.apps.security.service_auth import service_token
from videoroll.apps.publish_gateway import normalize_social_publish_meta
from videoroll.apps.outbox.dispatcher import _message_parts
from videoroll.db.models import OutboxEvent
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

db_url=os.environ["DATABASE_URL"]
assert db_url.endswith("/videoroll_test") and "127.0.0.1:" in db_url
secret=os.environ["INTERNAL_API_SECRET"]
token=service_token(SimpleNamespace(internal_api_secret=secret,development_mode=False))
base="http://127.0.0.1:18188"
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-sau"
def call(path, body=None, auth=token):
    headers={}
    if auth is not None: headers["X-Videoroll-Internal-Token"]=auth
    if body is not None: headers["Content-Type"]="application/json"
    req=Request(base+path,headers=headers,data=json.dumps(body).encode() if body is not None else None,
                method="POST" if body is not None else "GET")
    try: response=urlopen(req,timeout=10)
    except HTTPError as e:response=e
    with response:
        data=response.read()
        try: content=json.loads(data)
        except ValueError: content=data.decode(errors="replace")
        assert "SENSITIVE" not in str(content)
        return response.status, content
def spawn(enable):
    env=os.environ.copy()
    env["VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN"]="false"
    env["VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK"]="false"
    env.update(VIDEOROLL_V2_SAU_BIND="127.0.0.1:18188",VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX=str(enable).lower())
    proc=subprocess.Popen([str(exe)],env=env)
    for _ in range(80):
        assert proc.poll() is None
        try:
            if call("/health",auth=None)[0]==200:return proc
        except URLError:time.sleep(.1)
    proc.terminate();proc.wait()
    raise AssertionError("SAU service did not bind")
def stop(proc):
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)

ids={p:uuid.uuid4() for p in ("douyin","xiaohongshu","kuaishou")}
task=uuid.uuid4(); other=uuid.uuid4(); video="v2/render/"+str(task)+"/video.mp4"
cover="v2/render/"+str(task)+"/cover.png"
batch=uuid.uuid4(); batch2=uuid.uuid4(); mismatch=uuid.uuid4(); failed_task=uuid.uuid4()
meta={"title":"【熟肉】Test - Author","desc":"原作者：Author","tags":["#one","ONE","videoroll","two","three","four","five"]}
def body(p, **kw):
    return {"platform":p,"task_id":str(task),"account_id":str(ids[p]),
            "video":{"type":"storage","key":video},"cover":{"type":"storage","key":cover},
            "meta":meta,"platform_options":{},**kw}
def submit(p, **kw):return call("/sau/"+p+"/publish",body(p,**kw))
def db(): return psycopg.connect(db_url)
with db() as conn:
    for p, id in ids.items():
        conn.execute("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active,check_state) VALUES(%s,%s,'isolated-native-publish','dbplain:v1:SENSITIVE',true,'valid')",(id,p))
    for t in (task,other,failed_task):
        conn.execute("INSERT INTO tasks(id,source_type,source_license,status) VALUES(%s,'local','own','approved')",(t,))
    for kind,key in (("video_final",video),("cover_image",cover)):
        conn.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,%s,%s)",(uuid.uuid4(),task,kind,key))
    targets=json.dumps([{"platform":"douyin","account_id":str(ids["douyin"]),"key":"douyin:"+str(ids["douyin"])}])
    for b,t in ((batch,task),(batch2,task),(mismatch,other)):
        conn.execute("INSERT INTO publish_batches(id,task_id,expected_targets) VALUES(%s,%s,%s)",(b,t,targets))
    conn.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final',%s)",(uuid.uuid4(),failed_task,"test/rollback.mp4"))
proc=spawn(False)
try:
    assert submit("douyin")[0]==409
    assert call("/sau/douyin/publish",body("douyin"),auth=None)[0]==401
finally:stop(proc)
proc=spawn(True)
try:
    assert submit("douyin",force_retry=True)[0]==409
    assert submit("douyin",batch_id=str(mismatch))[0]==400
    assert submit("xiaohongshu",batch_id=str(batch))[0]==400
    assert submit("douyin",video={"type":"storage","key":"../secret"})[0]==400
    assert submit("douyin",video={"type":"storage","key":"other/video.mp4"})[0]==400
    assert submit("douyin",cover={"type":"storage","key":"other/cover.png"})[0]==400
    assert call("/sau/douyin/publish",body("kuaishou"))[0]==400
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(task,)).fetchone()[0]==0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:submit("douyin"),range(8)))
    assert all(code==200 for code,_ in results),results
    job=uuid.UUID(results[0][1]["job_id"])
    assert len({r["job_id"] for _,r in results})==1
    assert results[0][1]["state"]=="submitting"
    with db() as conn:
        actual=conn.execute("SELECT meta_json,state,operation_key FROM publish_jobs WHERE id=%s",(job,)).fetchone()
        assert actual[0]["meta"]==normalize_social_publish_meta(meta,"douyin"),actual
        assert actual[1:] == ("submitting","publish-job:"+str(job))
        events=conn.execute("SELECT id,event_type,aggregate_id,task_name,args_json,status FROM outbox_events WHERE operation_key=%s",(actual[2],)).fetchall()
        assert len(events)==1,events
        event=events[0]
        assert event[1:4]==("publish.dispatch",str(job),"social_publisher.process_job")
        assert event[4]=={"args":[str(job)],"queue":"social_publish"} and event[5]=="pending"
        assert conn.execute("SELECT status FROM tasks WHERE id=%s",(task,)).fetchone()[0]=="publishing"
    with Session(create_engine(db_url.replace("postgresql://","postgresql+psycopg://",1))) as session:
        args,kwargs,queue=_message_parts(session.get(OutboxEvent,event[0]))
        assert args==[str(job)] and kwargs=={} and queue=="social_publish"
    print("PASS: concurrent SAU requests produce one original job/outbox, Python metadata and dispatch envelope match")
    # Restart with pending job: same receipt, no duplicate dispatch.
    stop(proc);proc=spawn(True)
    assert submit("douyin")[1]["job_id"]==str(job)
    with db() as conn:
        conn.execute("UPDATE publish_jobs SET state='unknown',started_at=now() WHERE id=%s",(job,))
    assert submit("douyin")[1]["state"]=="unknown"
    assert submit("douyin",force_retry=True)[0]==409
    # Attach old unbatched unknown without a fresh publication.
    assert submit("douyin",batch_id=str(batch))[1]["job_id"]==str(job)
    assert submit("douyin",batch_id=str(batch2))[0]==409
    with db() as conn:
        conn.execute("UPDATE publish_jobs SET state='submitted',external_id='fixture-receipt' WHERE id=%s",(job,))
    code, copied=submit("douyin",batch_id=str(batch2))
    assert code==200 and copied["state"]=="submitted" and copied["external_id"]=="fixture-receipt" and copied["job_id"]!=str(job),copied
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM outbox_events WHERE task_name='social_publisher.process_job'").fetchone()[0]==1
    print("PASS: restart replay, unknown protection, prior batch receipt reuse with no extra browser dispatch")
    for p in ("xiaohongshu","kuaishou"):
        code, result=submit(p)
        assert code==200 and result["state"]=="submitting",result
        with db() as conn:
            saved=conn.execute("SELECT meta_json FROM publish_jobs WHERE id=%s",(result["job_id"],)).fetchone()[0]
            assert saved["meta"]==normalize_social_publish_meta(meta,p)
    # Force outbox insertion failure; publish_jobs and task state must roll back.
    with db() as conn:
        conn.execute("CREATE FUNCTION test_reject_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated injected outbox failure'; END; $$")
        conn.execute("CREATE TRIGGER test_reject_outbox BEFORE INSERT ON outbox_events FOR EACH ROW EXECUTE FUNCTION test_reject_outbox()")
    assert submit("kuaishou",task_id=str(failed_task),video={"key":"test/rollback.mp4"},cover=None)[0]==500
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(failed_task,)).fetchone()[0]==0
        assert conn.execute("SELECT status FROM tasks WHERE id=%s",(failed_task,)).fetchone()[0]=="approved"
        conn.execute("DROP TRIGGER test_reject_outbox ON outbox_events")
        conn.execute("DROP FUNCTION test_reject_outbox()")
    print("PASS: all three platforms, invalid media/account/batch, forced outbox failure rolls back whole transaction")
finally:
    stop(proc)
