#!/usr/bin/env python3
"""Standalone Rust SAU account read/revoke differential against original Python."""
import concurrent.futures
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from videoroll.apps.security.service_auth import service_token
from videoroll.apps.social_publisher.account_store import account_read
from videoroll.db.models import Account

db_url=os.environ["DATABASE_URL"]
assert db_url.endswith("/videoroll_test") and "127.0.0.1:" in db_url
secret=os.environ["INTERNAL_API_SECRET"]
token=service_token(SimpleNamespace(internal_api_secret=secret,development_mode=False))
assert token.startswith("v1.") and len(token)==67
base="http://127.0.0.1:18187"
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-sau"
assert exe.is_file(),exe
name_prefix="isolatedsau"+uuid.uuid4().hex[:10]
ids={name:uuid.uuid4() for name in ("douyin","xiaohongshu","kuaishou","bilibili")}
task=uuid.uuid4()
job=uuid.uuid4()
def postgresql():
    return psycopg.connect(db_url)
def call(path="/accounts",method="GET",authorization=token):
    headers={}
    if authorization is not None:
        headers["X-Videoroll-Internal-Token"]=authorization
    request=Request(base+path,method=method,headers=headers)
    try:response=urlopen(request,timeout=10)
    except HTTPError as e:response=e
    with response:
        data=response.read()
        try:content=json.loads(data)
        except ValueError:content=data.decode(errors="replace")
        assert "dbplain:v1:" not in str(content)
        assert "storage_state" not in str(content)
        assert secret not in str(content)
        return response.status,content

def spawn(overrides=None):
    env=os.environ.copy()
    env["VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN"]="false"
    env["VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK"]="false"
    env["VIDEOROLL_V2_SAU_BIND"]="127.0.0.1:18187"
    env.update(overrides or {})
    return subprocess.Popen([str(exe)],env=env,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
def stop(proc):
    proc.terminate()
    try:proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill();proc.wait(timeout=4)
def wait_for(proc):
    for _ in range(45):
        assert proc.poll() is None,"SAU Rust subprocess stopped prematurely"
        try:
            if call("/health",authorization=None)[0]==200:return
        except URLError:time.sleep(.15)
    raise AssertionError("Rust SAU standalone service failed to bind")
for overrides in (
    {"INTERNAL_API_SECRET":"videoroll-development-internal-secret"},
    {"VIDEOROLL_V2_SAU_BIND":"0.0.0.0:18187"},
):
    p=spawn(overrides)
    try:
        assert p.wait(timeout=6)!=0
    finally:
        if p.poll() is None:stop(p)
print("PASS: standalone Rust SAU rejects weak/default keys and unauthorised public listener")

with postgresql() as db:
    for platform,id in ids.items():
        db.execute("""
          INSERT INTO accounts
            (id,platform,name,secrets_encrypted,is_active,check_state,
             rotated_at,last_checked_at,last_check_message)
          VALUES(%s,%s::platform,%s,%s,true,'valid',now(),now(),%s)
        """,(id,platform,name_prefix+"_"+platform,
             'dbplain:v1:{"cookies":[{"name":"session","value":"SENSITIVE"}],"origins":[]}',
             "original check OK"))
    db.execute("UPDATE accounts SET check_state='checking' WHERE id=%s",
               (ids["xiaohongshu"],))
    db.execute("""
        INSERT INTO tasks(id,source_type,source_url,source_license,status)
        VALUES(%s,'youtube'::source_type,%s,'authorized'::source_license,'created'::task_status)
    """,(task,"https://www.youtube.com/watch?v=X1Y2Z3A4B5C"))
    db.execute("""
        INSERT INTO publish_jobs(id,task_id,account_id,platform,state)
        VALUES(%s,%s,%s,'kuaishou','submitting')
    """,(job,task,ids["kuaishou"]))
try:
    proc=spawn()
    try:
        wait_for(proc)
        assert call("/health",authorization=None)==(200,{"status":"ok","driver":"sau"})
        assert call("/accounts",authorization=None)[0]==401
        assert call("/accounts",authorization="wrong")[0]==403
        assert call("/accounts",authorization=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"])[0]==403
        assert call("/accounts?platform=bilibili")[0]==400
        assert call("/accounts?platform=unsupported")[0]==400
        assert call("/accounts/"+str(ids["douyin"]),method="DELETE",authorization=None)[0]==401
        print("PASS: Python HMAC service token grants accounts access; health remains public and internal v2 token cannot substitute")

        code,all_accounts=call("/accounts")
        assert code==200 and isinstance(all_accounts,list), (code,all_accounts)
        own=[x for x in all_accounts if x["name"].startswith(name_prefix)]
        assert {x["platform"] for x in own}=={"douyin","xiaohongshu","kuaishou"},own
        assert [x["platform"] for x in own]==sorted(x["platform"] for x in own),own
        with Session(create_engine(db_url.replace("postgresql://","postgresql+psycopg://",1))) as session:
            for item in own:
                original=account_read(session.get(Account,uuid.UUID(item["id"]))).model_dump(mode="json")
                for field in ("id","platform","name","is_active","check_state",
                              "last_check_message"):
                    assert item[field]==original[field],(field,item,original)
                for field in ("rotated_at","last_checked_at"):
                    parsed=datetime.fromisoformat(item[field].replace("Z","+00:00"))
                    expected=datetime.fromisoformat(original[field].replace("Z","+00:00"))
                    assert parsed==expected,(item[field],original[field])
        print("PASS: old Python account_read DTO equals Rust JSON including timestamps, all platforms sorted and Bilibili excluded")

        for platform in ("douyin","xiaohongshu","kuaishou"):
            code,items=call("/accounts?platform="+platform)
            assert code==200
            assert all(x["platform"]==platform for x in items),items
            assert any(x["id"]==str(ids[platform]) for x in items),items
        print("PASS: original per-platform filters preserve exact SAU account segregation")

        assert call("/accounts/"+str(uuid.uuid4()),method="DELETE")[0]==404
        assert call("/accounts/"+str(ids["bilibili"]),method="DELETE")[0]==404
        assert call("/accounts/"+str(ids["xiaohongshu"]),method="DELETE")[0]==409
        assert call("/accounts/"+str(ids["kuaishou"]),method="DELETE")[0]==409
        with postgresql() as db:
            rows=db.execute("""
                SELECT id,secrets_encrypted,is_active,check_state FROM accounts
                WHERE id IN (%s,%s,%s)
            """,(ids["xiaohongshu"],ids["kuaishou"],ids["bilibili"])).fetchall()
            assert all(s.startswith("dbplain:v1:") and active for _,s,active,_ in rows),rows
        print("PASS: busy account checking, submitting job and cross-platform account cannot be revoked")

        with postgresql() as db:
            db.execute("UPDATE publish_jobs SET state='unknown' WHERE id=%s",(job,))
            db.execute("UPDATE accounts SET check_state='valid' WHERE id=%s",
                       (ids["xiaohongshu"],))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            result=list(pool.map(lambda _:call("/accounts/"+str(ids["douyin"]),
                                              method="DELETE")[0],range(4)))
        assert result==[200]*4,result
        assert call("/accounts/"+str(ids["xiaohongshu"]),method="DELETE")== (200,{"ok":True})
        assert call("/accounts/"+str(ids["kuaishou"]),method="DELETE")== (200,{"ok":True})
        with postgresql() as db:
            records=db.execute("""
                SELECT id,is_active,secrets_encrypted,check_state,last_check_message,
                       rotated_at,last_checked_at FROM accounts
                WHERE id IN (%s,%s,%s)
            """,(ids["douyin"],ids["xiaohongshu"],ids["kuaishou"])).fetchall()
            assert len(records)==3,records
            for id,active,secret_value,state,message,rotated,checked in records:
                assert not active and secret_value=="" and state=="unchecked" and message is None,id
                assert rotated is not None and checked is not None
            prior=db.execute("SELECT state FROM publish_jobs WHERE id=%s",(job,)).fetchone()
            assert prior==("unknown",),prior
            untouched=db.execute("SELECT is_active,secrets_encrypted FROM accounts WHERE id=%s",
                                 (ids["bilibili"],)).fetchone()
            assert untouched[0] and untouched[1].startswith("dbplain:v1:"),untouched
        print("PASS: native revocation atomically clears secrets without changing old unknown publication receipt or Bilibili state")

        stop(proc)
        proc=spawn()
        wait_for(proc)
        code,records=call("/accounts?platform=douyin")
        assert code==200
        assert next(x for x in records if x["id"]==str(ids["douyin"]))["is_active"] is False
        assert call("/accounts/not-a-uuid",method="DELETE")[0] in (400,404,422)
        assert call("/login-sessions/"+str(uuid.uuid4()))[0]==404
        print("PASS: standalone SAU account state survives process restart; unmigrated login-sessions do not claim Rust support")
    finally:
        stop(proc)
finally:
    with postgresql() as db:
        db.execute("DELETE FROM publish_jobs WHERE id=%s",(job,))
        db.execute("DELETE FROM tasks WHERE id=%s",(task,))
        db.execute("DELETE FROM accounts WHERE id IN (%s,%s,%s,%s)",
                   tuple(ids.values()))
