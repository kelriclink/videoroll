#!/usr/bin/env python3
"""Original Python/Rust desktop grant compatibility in disposable PostgreSQL."""
import hashlib
import concurrent.futures
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from videoroll.apps.orchestrator_api.admin_auth_store import (
    encode_password_hash,mint_device_cookie_value,verify_device_cookie_value
)
from videoroll.apps.security.service_auth import admin_cookie_secret
from videoroll.apps.orchestrator_api.desktop_grants import (
    create_desktop_grant,desktop_session_fingerprint,_consume_desktop_grant
)

db_url=os.environ["DATABASE_URL"]
assert db_url.endswith("/videoroll_test") and "127.0.0.1:" in db_url
BASE="http://127.0.0.1:18080"
internal=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
settings=SimpleNamespace(
    internal_api_secret=os.environ["INTERNAL_API_SECRET"],
    development_mode=False,
)
secret=admin_cookie_secret(settings)
with psycopg.connect(db_url) as db:
    old=db.execute("SELECT value_json->>'password_hash' FROM app_settings WHERE key='admin.auth'").fetchone()
    password_hash=old[0] if old else None
    if not password_hash:
        password_hash=encode_password_hash("isolated-desktop-password-123")
        db.execute("""
          INSERT INTO app_settings(key,value_json,version)
          VALUES('admin.auth',jsonb_build_object('password_hash',%s::text),1)
          ON CONFLICT(key) DO UPDATE SET value_json=
            jsonb_set(app_settings.value_json,'{password_hash}',to_jsonb(%s::text))
        """,(password_hash,password_hash))
device=mint_device_cookie_value(internal_secret=secret,password_hash=password_hash)
other=mint_device_cookie_value(internal_secret=secret,password_hash=password_hash)
assert device!=other
assert verify_device_cookie_value(device,internal_secret=secret,password_hash=password_hash)
res_a=str(uuid.uuid4())
res_b=str(uuid.uuid4())

def call(path,method="GET",body=None,cookie=device,headers=None,service_token=False):
    h=dict(headers or {})
    if cookie is not None:h["Cookie"]="videoroll_admin_device="+cookie
    if service_token:h["x-videoroll-v2-internal-token"]=internal
    if body is not None:h["Content-Type"]="application/json"
    req=Request(BASE+path,method=method,headers=h,
                data=json.dumps(body).encode() if body is not None else None)
    try:response=urlopen(req,timeout=15)
    except HTTPError as error:response=error
    with response:
        raw=response.read()
        try:value=json.loads(raw)
        except ValueError:value=raw.decode(errors="replace")
        return response.status,value

def grant(kind="login", resource=res_a, cookie=device):
    code,body=call("/api/v1/desktop/grants",method="POST",
                   body={"desktop_type":kind,"resource_id":resource},
                   cookie=cookie)
    assert code==200,(code,body)
    assert body["desktop_type"]==kind and body["resource_id"]==resource,body
    assert body["reconnect_limit"]==8,body
    assert len(body["token"])==43,body
    return body

def scoped(data,path,cookie=device,resource=None,root=True):
    uri=f"{path}?grant={data['token']}&resource={resource or data['resource_id']}"
    url="/desktop/authorize" if root else "/api/v1/desktop/authorize"
    return call(url,headers={"x-original-uri":uri},cookie=cookie)

def row(data):
    digest=hashlib.sha256(data["token"].encode()).hexdigest()
    with psycopg.connect(db_url) as db:
        return db.execute("""
            SELECT subject,scope_json,status,expires_at,consumed_at,last_error
            FROM desktop_access_grants WHERE token_hash=%s
        """,(digest,)).fetchone()

assert call("/api/v1/desktop/grants",method="POST",body=None,cookie=None,
            service_token=True)[0]==401
assert call("/api/v1/desktop/grants",method="POST",
            body={"desktop_type":"login","resource_id":res_a},
            cookie=None,service_token=True)[0]==401
assert call("/desktop/authorize",cookie=None,service_token=True,
    headers={"x-original-uri":"/social-login/vnc.html"})[0]==401
assert call("/api/v1/desktop/grants",method="POST",
            body={"desktop_type":"invalid","resource_id":res_a})[0]==400
assert call("/api/v1/desktop/grants",method="POST",
            body={"desktop_type":"login","resource_id":"not-uuid"})[0]==400
print("PASS: Python device cookie mandatory; admin/internal service token cannot issue or consume desktop grants")

created=grant()
saved=row(created)
assert saved and saved[0]==desktop_session_fingerprint(device),saved
assert saved[1]=={
    "desktop_type":"login","resource_id":res_a,
    "session_fingerprint":saved[0],"reconnect_limit":8,"reconnect_count":0
},saved
assert saved[2]=="active" and saved[4] is None
assert created["token"] not in json.dumps(saved,default=str)
assert saved[3]>datetime.now(timezone.utc)
assert saved[3]<datetime.now(timezone.utc)+timedelta(seconds=310)
print("PASS: Rust writes original desktop_access_grants schema, cookie fingerprint, SHA256-only token and five-minute TTL")

for uri in [
    "/other/desktop","/social-login/../admin",
    "/social-login/%2e%2e/admin",
    "/social-login/websockify?resource="+res_a,
    "/social-publish/websockify?grant="+created["token"]+"&resource="+res_a
]:
    assert call("/desktop/authorize",headers={"x-original-uri":uri})[0]==403,uri
assert scoped(created,"/social-login/vnc.html",cookie=other)[0]==403
assert scoped(created,"/social-login/vnc.html",resource=res_b)[0]==403
assert row(created)[1]["reconnect_count"]==0
print("PASS: Nginx original URI, session scope and resource/type mismatch fail closed")

for _ in range(13):
    assert scoped(created,"/social-login/vnc.html")[0]==204
    assert scoped(created,"/social-login/include/app.js",root=False)[0]==204
assert row(created)[1]["reconnect_count"]==0
assert row(created)[2]=="active"
print("PASS: repeated noVNC assets return HTTP 204 without spending reconnect quota")

orm_url=db_url.replace("postgresql://","postgresql+psycopg://",1)
engine=create_engine(orm_url)
with Session(engine) as session:
    accepted=_consume_desktop_grant(
        session,admin_session=device,desktop_type="login",
        resource_id=res_a,token=created["token"],consume_reconnect=True)
    assert accepted
assert row(created)[1]["reconnect_count"]==1
for i in range(7):
    assert scoped(created,"/social-login/websockify")[0]==204,i
assert row(created)[1]["reconnect_count"]==8
assert row(created)[2]=="consumed"
assert scoped(created,"/social-login/websockify")[0]==403
assert scoped(created,"/social-login/vnc.html")[0]==403
print("PASS: Python can consume Rust grant; Rust completes reconnect budget atomically and blocks ninth")

with Session(engine) as session:
    original=create_desktop_grant(session,device,"publish",res_b)
original_data={"token":original.token,"resource_id":original.resource_id}
assert scoped(original_data,"/social-publish/vnc.html")[0]==204
assert scoped(original_data,"/social-publish/websockify")[0]==204
assert row(original_data)[1]["reconnect_count"]==1
print("PASS: old Python-created original-table grant is accepted and consumed by Rust root Nginx endpoint")

expired=grant("publish",res_a)
with psycopg.connect(db_url) as db:
    db.execute("""UPDATE desktop_access_grants SET expires_at=now()-interval '2 seconds'
         WHERE token_hash=%s""",(hashlib.sha256(expired["token"].encode()).hexdigest(),))
assert scoped(expired,"/social-publish/vnc.html")[0]==403
assert row(expired)[2]=="expired" and row(expired)[5]=="expired"
print("PASS: expiration marks original row expired and rejects static assets")

concurrent_grant=grant("login",res_b)
def handshake(_):
    return scoped(concurrent_grant,"/social-login/websockify")[0]
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
    statuses=list(executor.map(handshake,range(16)))
assert statuses.count(204)==8,statuses
assert statuses.count(403)==8,statuses
assert row(concurrent_grant)[1]["reconnect_count"]==8
assert row(concurrent_grant)[2]=="consumed"
print("PASS: 16 simultaneous WebSocket auth subrequests cannot exceed eight connections or corrupt original row")
