#!/usr/bin/env python3
"""Native account import/check worker using an isolated DB and a fake SAU CLI."""
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
from videoroll.apps.social_publisher.account_store import account_read, canonicalize_storage_state
from videoroll.db.models import Account

db_url=os.environ["DATABASE_URL"]
assert db_url.endswith("/videoroll_test") and "127.0.0.1:" in db_url
secret=os.environ["INTERNAL_API_SECRET"]
token=service_token(SimpleNamespace(internal_api_secret=secret,development_mode=False))
base="http://127.0.0.1:18189"
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-sau"
fixture={"cookies":[{"name":"session","value":"SENSITIVE_FIXTURE"}]}
raw=json.dumps(fixture).encode()

def db():return psycopg.connect(db_url)
def call(path, method="GET", data=None, mime=None, auth=token):
    headers={}
    if auth is not None:headers["X-Videoroll-Internal-Token"]=auth
    if mime:headers["Content-Type"]=mime
    req=Request(base+path,data=data,method=method,headers=headers)
    try:response=urlopen(req,timeout=10)
    except HTTPError as e:response=e
    with response:
        data=response.read()
        try:content=json.loads(data)
        except ValueError:content=data.decode(errors="replace")
        assert "SENSITIVE_FIXTURE" not in str(content) and "dbplain:v1:" not in str(content)
        return response.status,content
def upload(platform,name,data=raw,auth=token):
    boundary="vr2-isolated-sau-import-boundary"
    payload=(f"--{boundary}\r\nContent-Disposition: form-data; name=\"account_name\"\r\n\r\n{name}\r\n"
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"state.json\"\r\n"
             "Content-Type: application/json\r\n\r\n").encode()+data+f"\r\n--{boundary}--\r\n".encode()
    return call("/accounts/"+platform,"POST",payload,"multipart/form-data; boundary="+boundary,auth)
def recheck(id):return call(f"/accounts/{id}/check","POST",b"")
def wait_for(predicate, limit=10):
    deadline=time.monotonic()+limit
    while time.monotonic()<deadline:
        result=predicate()
        if result:return result
        time.sleep(.05)
    raise AssertionError("timed out waiting for isolated check state")
def state(id):
    with db() as conn:
        return conn.execute("SELECT check_state FROM accounts WHERE id=%s",(id,)).fetchone()[0]
def opstate(id):
    with db() as conn:
        return conn.execute("SELECT status,result_json FROM operation_inbox WHERE id=%s",(id,)).fetchone()
def wait_state(id, wanted):return wait_for(lambda:state(id)==wanted)
def stop(proc, kill=False):
    if proc.poll() is None:
        (proc.kill if kill else proc.terminate)()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)

with tempfile.TemporaryDirectory(prefix="vr2-sau-check-") as tmp:
    root=Path(tmp);cookies=root/"cookies";cookies.mkdir(mode=0o700)
    mock=root/"sau"
    mock.write_text("""#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
assert len(sys.argv)==5 and sys.argv[2:4]==['check','--account']
platform,name=sys.argv[1],sys.argv[4]
cookie=Path('cookies')/f'{platform}_{name}.json'
assert cookie.stat().st_mode & 0o777 == 0o600
state=json.loads(cookie.read_text())
with open('invocations','a') as f:f.write(f'{platform}:{name}\\n')
if name=='hold':
    while not Path('release').exists():time.sleep(.05)
if name=='timeout':time.sleep(30)
if name=='invalid':
    print('SENSITIVE_FIXTURE',file=sys.stderr)
    sys.exit(1)
state['cookies'][0]['value']='SENSITIVE_FIXTURE_REFRESHED'
cookie.write_text(json.dumps(state))
""")
    mock.chmod(0o700)
    def spawn(enable=True,port=18189,wait=True):
        env=os.environ.copy()
        env["VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN"]="false"
        env.update(VIDEOROLL_V2_SAU_BIND=f"127.0.0.1:{port}",
                   VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK=str(enable).lower(),
                   VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX="false",
                   SAU_RUNTIME_DIR=str(root),SAU_COOKIES_DIR=str(cookies),
                   SAU_EXECUTABLE=str(mock),SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS="3")
        proc=subprocess.Popen([str(exe)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if wait:
            def ready():
                assert proc.poll() is None,"native checker stopped prematurely"
                try:return call("/health",auth=None)==(200,{"status":"ok","driver":"sau"})
                except URLError:return False
            try:wait_for(ready)
            except BaseException:stop(proc);raise
        return proc

    proc=spawn(False)
    try:assert upload("douyin","disabled")[0]==409
    finally:stop(proc)
    proc=spawn()
    try:
        other=spawn(port=18190,wait=False)
        try:assert other.wait(timeout=5)!=0
        finally:stop(other)
        assert upload("douyin","unauth",auth=None)[0]==401
        assert upload("bilibili","unsupported")[0]==400
        assert upload("douyin","../escape")[0]==400
        assert upload("douyin","invalidjson",b"{}")[0]==400
        assert upload("douyin","oversize",b" "* (1024*1024+1))[0] in (400,413)
        code,res=upload("douyin","hold")
        assert code==200 and res["account"]["check_state"]=="queued",res
        held=res["account"]["id"];held_op=res["check_job_id"]
        wait_state(held,"checking")
        assert recheck(held)[0]==409
        assert upload("douyin","hold")[0]==409
        assert call("/accounts/"+held,"DELETE")[0]==409
        # While one checker is held, repeat a queued request concurrently.
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:upload("xiaohongshu","dedupe"),range(6)))
        assert all(code==200 for code,_ in results),results
        assert len({r["account"]["id"] for _,r in results})==1
        assert len({r["check_job_id"] for _,r in results})==1
        r=results[0][1];aid=r["account"]["id"];op=r["check_job_id"]
        assert recheck(aid)[1]["check_job_id"]==op
        with db() as conn:
            stored=conn.execute("SELECT secrets_encrypted FROM accounts WHERE id=%s",(aid,)).fetchone()[0]
            assert stored=="dbplain:v1:"+canonicalize_storage_state(raw)
            assert conn.execute("SELECT request_json,status FROM operation_inbox WHERE id=%s",(op,)).fetchone()==({"account_id":aid},"pending")
        (root/"release").touch()
        wait_state(held,"valid");wait_state(aid,"valid")
        assert opstate(op)==("completed",{"status":"valid"})
        assert not list(cookies.glob("*.json"))
        with Session(create_engine(db_url.replace("postgresql://","postgresql+psycopg://",1))) as session:
            expected=account_read(session.get(Account,uuid.UUID(aid))).model_dump(mode="json")
        code,all_accounts=call("/accounts?platform=xiaohongshu")
        actual=next(item for item in all_accounts if item["id"]==aid)
        for key in ("id","platform","name","is_active","check_state","last_check_message"):
            assert actual[key]==expected[key]
        with db() as conn:
            stored=conn.execute("SELECT secrets_encrypted FROM accounts WHERE id=%s",(aid,)).fetchone()[0]
            assert "SENSITIVE_FIXTURE_REFRESHED" in stored
        assert (root/"invocations").read_text().count("xiaohongshu:dedupe\n")==1
        print("PASS: original DTO and credential format, concurrent import/recheck dedupe, busy fences, cookie refresh and cleanup")

        for account_name in ("invalid","timeout"):
            code,r=upload("kuaishou",account_name)
            assert code==200,r
            wait_state(r["account"]["id"],"invalid")
            assert opstate(r["check_job_id"])==("completed",{"status":"invalid"})
            assert not list(cookies.glob("*.json"))
        # A stale file is never overwritten or automatically removed.
        stale=cookies/"douyin_stale.json";stale.write_text("retained-stale-fixture")
        code,r=upload("douyin","stale")
        assert code==200,r
        wait_state(r["account"]["id"],"error")
        assert stale.read_text()=="retained-stale-fixture"
        stale.unlink()
        print("PASS: failed, timed-out and stale-cookie checks are terminal without leaked browser output")

        # Stop the API mid-check, then inject the elapsed lease in the
        # disposable DB (avoids waiting for the actual 63 second lease).
        (root/"release").unlink()
        code,r=upload("douyin","hold")
        assert code==200,r
        wait_state(held,"checking")
        wait_for(lambda:(cookies/"douyin_hold.json").exists())
        interrupted=r["check_job_id"]
        stop(proc,kill=True)
        with db() as conn:
            conn.execute("UPDATE operation_inbox SET lease_until=now()-interval '1 second' WHERE id=%s",(interrupted,))
        proc=spawn()
        wait_state(held,"error")
        assert opstate(interrupted)==("completed",{"status":"error"})
        # Revoke only after recovery; old operation cannot re-enable account.
        assert call("/accounts/"+held,"DELETE")[0]==200
        assert recheck(held)[0]==400
        with db() as conn:
            assert conn.execute("SELECT is_active,secrets_encrypted,check_state FROM accounts WHERE id=%s",(held,)).fetchone()==(False,"","unchecked")
        print("PASS: expired in-flight lease recovery fails closed after process kill; no credential resurrection")
    finally:
        stop(proc)
