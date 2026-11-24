#!/usr/bin/env python3
"""Native login lifecycle against disposable PostgreSQL and fake browser CLI."""
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
from videoroll.apps.security.service_auth import service_token
from videoroll.apps.social_publisher.account_store import canonicalize_storage_state
from videoroll.apps.social_publisher.schemas import SocialLoginSessionRead

db_url=os.environ["DATABASE_URL"]
assert db_url.endswith("/videoroll_test") and "127.0.0.1:" in db_url
token=service_token(SimpleNamespace(internal_api_secret=os.environ["INTERNAL_API_SECRET"],development_mode=False))
base="http://127.0.0.1:18191"
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-sau"
fixture={"cookies":[{"name":"session","value":"SENSITIVE_LOGIN_FIXTURE"}],"origins":[]}
raw=json.dumps(fixture).encode()
browser_url="/social-login/vnc.html?autoconnect=1&resize=scale&path=/social-login/websockify"

def db():return psycopg.connect(db_url)
def call(path,method="GET",data=None,mime=None,auth=token):
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
        assert "SENSITIVE_LOGIN_FIXTURE" not in str(content) and "dbplain:v1:" not in str(content)
        return response.status,content
def start(p,n,auth=token):
    return call("/login-sessions/"+p,"POST",json.dumps({"account_name":n}).encode(),"application/json",auth)
def get(id):return call("/login-sessions/"+id)
def cancel(id):return call("/login-sessions/"+id,"DELETE")
def upload(p,n):
    b="vr2-sau-login-test-boundary"
    payload=(f"--{b}\r\nContent-Disposition: form-data; name=\"account_name\"\r\n\r\n{n}\r\n"
      f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"state.json\"\r\nContent-Type: application/json\r\n\r\n").encode()+raw+f"\r\n--{b}--\r\n".encode()
    return call("/accounts/"+p,"POST",payload,"multipart/form-data; boundary="+b)
def wait_for(predicate,limit=12):
    deadline=time.monotonic()+limit
    while time.monotonic()<deadline:
        r=predicate()
        if r:return r
        time.sleep(.05)
    raise AssertionError("timed out waiting for login session")
def wait_state(id,wanted):
    def predicate():
        code,res=get(id)
        assert code==200
        if res["state"] in ("failed","canceled","succeeded") and res["state"]!=wanted:
            raise AssertionError((wanted,res))
        return res if res["state"]==wanted else None
    return wait_for(predicate)
def stop(proc,kill=False):
    if proc.poll() is None:
        (proc.kill if kill else proc.terminate)()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
def account(p,n):
    with db() as conn:
        return conn.execute("SELECT id,secrets_encrypted,is_active,check_state,last_check_message FROM accounts WHERE platform::text=%s AND name=%s",(p,n)).fetchone()

with tempfile.TemporaryDirectory(prefix="vr2-sau-login-") as tmp:
    root=Path(tmp);cookies=root/"cookies";cookies.mkdir(mode=0o700)
    mock=root/"sau"
    mock.write_text("""#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
p,mode,flag,n,*rest=sys.argv[1:]
assert flag=='--account'
cookie=Path('cookies')/f'{p}_{n}.json'
with open('invocations','a') as f:f.write(f'{p}:{mode}:{n}\\n')
if mode=='check':
    assert cookie.stat().st_mode&0o777==0o600
    if n=='checkhold':
        while not Path('checkrelease').exists():time.sleep(.05)
    sys.exit(0)
assert mode=='login' and rest==['--headed'] and os.environ['DISPLAY']==':991'
assert not cookie.exists()
if n=='invalid':
    cookie.write_text('{}')
elif n=='missing':
    sys.exit(0)
elif n=='failure':
    print('SENSITIVE_LOGIN_FIXTURE',file=sys.stderr);sys.exit(1)
else:
    cookie.write_text(json.dumps({'cookies':[{'name':'session','value':'SENSITIVE_LOGIN_FIXTURE'}],'origins':[]}))
    assert cookie.stat().st_mode&0o777==0o600
if n in ('hold','changed','interrupted'):
    while not Path('release').exists():time.sleep(.05)
if n=='timeout':time.sleep(30)
""")
    mock.chmod(0o700)
    def spawn(login=True,check=True,wait=True):
        env=os.environ.copy()
        env.update(VIDEOROLL_V2_SAU_BIND="127.0.0.1:18191",
          VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK=str(check).lower(),
          VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN=str(login).lower(),
          VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX="true",
          SAU_RUNTIME_DIR=str(root),SAU_COOKIES_DIR=str(cookies),
          SAU_EXECUTABLE=str(mock),SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS="5",
          SOCIAL_LOGIN_TIMEOUT_SECONDS="5",SOCIAL_LOGIN_DISPLAY=":991",SOCIAL_LOGIN_BROWSER_URL=browser_url)
        proc=subprocess.Popen([str(exe)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if wait:
            def ready():
                assert proc.poll() is None,"native login service stopped"
                try:return call("/health",auth=None)==(200,{"status":"ok","driver":"sau"})
                except URLError:return False
            try:wait_for(ready)
            except BaseException:stop(proc);raise
        return proc

    proc=spawn(login=True,check=False,wait=False)
    try:assert proc.wait(timeout=5)!=0
    finally:stop(proc)
    proc=spawn(login=False,check=False)
    try:assert start("douyin","disabled")[0]==409
    finally:stop(proc)
    proc=spawn()
    try:
        assert start("douyin","noauth",auth=None)[0]==401
        assert start("bilibili","bad")[0]==400
        assert start("douyin","../escape")[0]==400
        assert get(str(uuid.uuid4()))[0]==404
        assert cancel(str(uuid.uuid4()))[0]==404
        # First login creates the original account only after the browser succeeds.
        code,res=start("douyin","  success ")
        assert code==200 and res["state"]=="starting",res
        assert set(res)==set(SocialLoginSessionRead.model_fields)
        SocialLoginSessionRead.model_validate(res)
        assert res["account_name"]=="success" and res["browser_url"]==browser_url
        aid=wait_state(res["id"],"succeeded")
        assert aid["finished_at"]
        original=account("douyin","success")
        assert original[1:]==("dbplain:v1:"+canonicalize_storage_state(raw),True,"valid","browser login completed"),original[2:]
        assert cancel(res["id"])[1]==aid
        assert not list(cookies.glob("*.json"))
        print("PASS: original login DTO and noVNC URL, account saved only on successful browser completion, terminal cancellation idempotent")

        # Login against an existing account blocks mutation and submission.
        existing=uuid.uuid4()
        with db() as conn:
            conn.execute("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active,check_state,rotated_at) VALUES(%s,'douyin','hold','dbplain:v1:old-fixture',true,'valid',now())",(existing,))
            task=uuid.uuid4()
            conn.execute("INSERT INTO tasks(id,source_type,source_license,status) VALUES(%s,'local','own','approved')",(task,))
            conn.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final','test/login.mp4')",(uuid.uuid4(),task))
        code,res=start("douyin","hold");assert code==200,res
        sid=res["id"]
        wait_state(sid,"running")
        wait_for(lambda:(cookies/"douyin_hold.json").exists())
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:start("kuaishou","another"),range(6)))
        assert all(c==409 for c,_ in results),results
        assert upload("douyin","hold")[0]==409
        assert call(f"/accounts/{existing}/check","POST",b"")[0]==409
        assert call(f"/accounts/{existing}","DELETE")[0]==409
        publish={"platform":"douyin","task_id":str(task),"account_id":str(existing),"video":{"key":"test/login.mp4"},"meta":{"title":"fixture"}}
        assert call("/sau/douyin/publish","POST",json.dumps(publish).encode(),"application/json")[0]==409
        assert cancel(sid)[1]["state"] in ("canceling","canceled")
        wait_state(sid,"canceled")
        assert account("douyin","hold")[1]=="dbplain:v1:old-fixture"
        assert not list(cookies.glob("*.json"))
        with db() as conn:
            assert conn.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(task,)).fetchone()[0]==0
        print("PASS: concurrent login exclusion, import/recheck/revoke/publish fences, cancellation cannot overwrite an existing account")

        # Pending session cancellation while native checker holds the execution slot.
        code,r=upload("kuaishou","checkhold");assert code==200,r
        wait_for(lambda:(cookies/"kuaishou_checkhold.json").exists())
        code,r=start("xiaohongshu","pending");assert code==200,r
        assert cancel(r["id"])[1]["state"]=="canceled"
        (root/"checkrelease").touch()
        wait_for(lambda:account("kuaishou","checkhold")[3]=="valid")
        assert get(r["id"])[1]["state"]=="canceled" and account("xiaohongshu","pending") is None
        assert "xiaohongshu:login:pending" not in (root/"invocations").read_text()

        # The same original account ID is retained on a successful re-login.
        code,r=start("douyin","success");assert code==200,r
        wait_state(r["id"],"succeeded")
        assert account("douyin","success")[0]==original[0]
        for n in ("invalid","missing","failure","timeout"):
            code,r=start("kuaishou",n);assert code==200,r
            wait_state(r["id"],"failed")
            assert account("kuaishou",n) is None and not list(cookies.glob("*.json"))
        stale=cookies/"douyin_stale.json";stale.write_text("existing file")
        code,r=start("douyin","stale");assert code==200,r
        wait_state(r["id"],"failed")
        assert stale.read_text()=="existing file";stale.unlink()
        print("PASS: pending cancellation, same-ID credential rotation, missing/invalid cookie, CLI failure/timeout and stale-file guard")

        # Simulate out-of-band account modification; old session must not overwrite it.
        changed=uuid.uuid4()
        with db() as conn:
            conn.execute("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active,check_state,rotated_at) VALUES(%s,'xiaohongshu','changed','dbplain:v1:before',true,'valid',now())",(changed,))
        code,r=start("xiaohongshu","changed");assert code==200,r
        wait_for(lambda:(cookies/"xiaohongshu_changed.json").exists())
        with db() as conn:
            conn.execute("UPDATE accounts SET secrets_encrypted='dbplain:v1:newer',rotated_at=now() WHERE id=%s",(changed,))
        (root/"release").touch()
        wait_state(r["id"],"failed")
        assert account("xiaohongshu","changed")[1]=="dbplain:v1:newer"
        (root/"release").unlink()

        # Kill native API while browser is running; recovery must not replay login.
        code,r=start("kuaishou","interrupted");assert code==200,r
        interrupted=r["id"]
        wait_for(lambda:(cookies/"kuaishou_interrupted.json").exists())
        stop(proc,kill=True)
        with db() as conn:
            conn.execute("UPDATE operation_inbox SET lease_until=now()-interval '1 second' WHERE id=%s",(interrupted,))
        proc=spawn()
        failed=wait_state(interrupted,"failed")
        assert "interrupted" in failed["message"]
        assert account("kuaishou","interrupted") is None
        assert (cookies/"kuaishou_interrupted.json").exists()
        assert (root/"invocations").read_text().count("kuaishou:login:interrupted\n")==1
        assert get(res["id"])[1]["state"]=="canceled"
        with db() as conn:
            assert conn.execute("SELECT count(*) FROM operation_inbox WHERE runtime_engine='rust-sau-login' AND status IN ('pending','processing')").fetchone()[0]==0
            # Session state records never store credential contents.
            assert conn.execute("SELECT count(*) FROM operation_inbox WHERE runtime_engine='rust-sau-login' AND (request_json::text LIKE '%SENSITIVE_LOGIN_FIXTURE%' OR result_json::text LIKE '%SENSITIVE_LOGIN_FIXTURE%')").fetchone()[0]==0
        print("PASS: out-of-band credential modification fence, process-kill recovery, durable session history, no cookie leakage")
    finally:
        stop(proc)
