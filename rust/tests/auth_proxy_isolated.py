"""Isolated real HTTP + PostgreSQL authentication proxy/limiter checks."""
import base64
import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import psycopg

url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
base="http://127.0.0.1:18195"
password="isolated-password-123"
def b64(x):return base64.urlsafe_b64encode(x).rstrip(b"=").decode()
salt=b"isolated-fixture"
encoded=f"pbkdf2_sha256$1000${b64(salt)}${b64(hashlib.pbkdf2_hmac('sha256',password.encode(),salt,1000))}"
def db():return psycopg.connect(url)
with db() as c:
    c.execute("INSERT INTO app_settings(key,value_json) VALUES('admin.auth',%s::jsonb) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({"password_hash":encoded}),))
def start(cidrs="",hosts=""):
    proc=subprocess.Popen([str(exe),"api"],env={**os.environ,"VIDEOROLL_V2_BIND":"127.0.0.1:18195","TRUSTED_PROXY_CIDRS":cidrs,"TRUSTED_PROXY_HOSTS":hosts})
    try:
        for _ in range(80):
            try:
                with urlopen(base+"/ready",timeout=1) as r:
                    if r.status==200:return proc
            except URLError:time.sleep(.1)
        raise AssertionError("API did not start")
    except BaseException:
        stop(proc)
        raise
def stop(proc):
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
def login(chain="",pw="incorrect",real="203.0.113.99",route="/api/auth/login"):
    req=Request(base+route,data=json.dumps({"password":pw}).encode(),headers={"Content-Type":"application/json","X-Forwarded-For":chain,"X-Real-IP":real})
    try:r=urlopen(req,timeout=10)
    except HTTPError as e:r=e
    with r:return r.status,r.headers.get("Retry-After"),r.headers.get("Set-Cookie")
def key(ip,endpoint="login"):
    return hashlib.sha256((ip if endpoint=="login" else "setup:"+ip).encode()).hexdigest()
def count(ip,endpoint="login"):
    with db() as c:
        row=c.execute("SELECT failures FROM auth_login_limits WHERE source_key=%s",(key(ip,endpoint),)).fetchone()
        return row[0] if row else None
def setup(ip,secret,pw=password,route="/api/auth/setup"):
    req=Request(base+route,data=json.dumps({"password":pw}).encode(),headers={
        "Content-Type":"application/json","X-Forwarded-For":ip,
        "X-Videoroll-Admin-Bootstrap":secret,"X-Request-ID":"sensitive-request-id",
        "User-Agent":"sensitive-header-value","Cookie":"token=sensitive-cookie"})
    try:r=urlopen(req,timeout=15)
    except HTTPError as e:r=e
    with r:return r.status,r.headers.get("Retry-After")
proc=start()
try:
    for i in range(4):
        assert login(f"192.0.2.{i+1}")[0]==401
    assert login("192.0.2.5")[:2]==(429,"120")
    assert login("192.0.2.6")[:2]==(429,"240")
    assert count("127.0.0.1")==6 and count("192.0.2.5") is None
finally:stop(proc)
proc=start("127.0.0.1/32,10.0.0.0/8","localhost")
try:
    for i in range(4):
        assert login(f"203.0.113.{i+1}, 192.0.2.10, 10.0.0.2",route="/api/v1/auth/login")[0]==401
    assert login("203.0.113.5,192.0.2.10,10.0.0.2")[:2]==(429,"120")
    assert count("192.0.2.10")==5 and count("203.0.113.5") is None
    assert login("192.0.2.11",pw=password)[0]==200
    assert count("192.0.2.11") is None
    # A correct fifth attempt is still permitted, then clears the bucket.
    for _ in range(4):assert login("192.0.2.12")[0]==401
    code,retry,cookie=login("192.0.2.12",pw=password)
    assert code==200 and cookie and count("192.0.2.12") is None
    # Invalid chain and X-Real-IP alone cannot create a fresh bucket.
    assert login("invalid,10.0.0.2")[0]==429
    assert login("")[0]==429
    # A real time-expired block resets.
    with db() as c:
        c.execute("UPDATE auth_login_limits SET blocked_until=now()-interval '1 second' WHERE source_key=%s",(hashlib.sha256(b"192.0.2.10").hexdigest(),))
    assert login("192.0.2.10")[0]==401 and count("192.0.2.10")==1
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:login("192.0.2.13")[0],range(8)))
    assert results.count(401)==4 and results.count(429)==4,results
    assert count("192.0.2.13")==8
    # Saturating exponential backoff is bounded to one hour.
    with db() as c:
        c.execute("UPDATE auth_login_limits SET failures=1000 WHERE source_key=%s",(hashlib.sha256(b"192.0.2.13").hexdigest(),))
    assert login("192.0.2.13")[:2]==(429,"3600")
    # Setup has an independent bucket; a locked login is not a setup lock.
    for i in range(4):
        assert setup("192.0.2.13","sensitive-wrong-bootstrap",route="/api/v1/auth/setup")[0]==401
    assert setup("192.0.2.13","sensitive-wrong-bootstrap")== (429,"120")
    assert count("192.0.2.13","setup")==5 and count("192.0.2.13")==1001
    secret=os.environ["ADMIN_BOOTSTRAP_SECRET"]
    with db() as c:
        c.execute("DELETE FROM app_settings WHERE key='admin.auth'")
    assert login("192.0.2.21")[0]==400
    assert setup("192.0.2.22",secret,"short")[0]==400
    # An unavailable audit store must not consume the bootstrap or set the
    # password. Inject a constraint failure in the real success transaction.
    with db() as c:
        c.execute("ALTER TABLE security_audit_events ADD CONSTRAINT fixture_audit_failure CHECK(event_type<>'admin.setup.success')")
    assert setup("192.0.2.23",secret)==(503,None)
    with db() as c:
        assert c.execute("SELECT count(*) FROM app_settings WHERE key='admin.auth'").fetchone()[0]==0
        c.execute("ALTER TABLE security_audit_events DROP CONSTRAINT fixture_audit_failure")
    # Two simultaneous valid requests cannot consume the same bootstrap.
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(lambda ip:setup(ip,secret)[0],["192.0.2.24","192.0.2.25"]))
    assert sorted(result)==[201,409],result
    winner="192.0.2.24" if result[0]==201 else "192.0.2.25"
    assert count(winner,"setup") is None
    assert login("192.0.2.26",pw=password)[0]==200
    with db() as c:
        stored=c.execute("SELECT value_json FROM app_settings WHERE key='admin.auth'").fetchone()[0]
        assert stored["bootstrap_consumed"] is True
        audits=c.execute("SELECT event_type,outcome,source_ip,payload_json,error_code,request_id,error_message,actor_type FROM security_audit_events").fetchall()
        assert sum(r[0]=="admin.setup.success" for r in audits)==1
        assert any(r[:5]==("admin.setup.throttle","throttled","192.0.2.13",{"attempts":5,"retry_after":120},"rate_limited") for r in audits)
        assert any(r[0]=="admin.login.failure" and r[4]=="password_not_set" for r in audits)
        assert any(r[0]=="admin.setup.failure" and r[4]=="invalid_password" for r in audits)
        assert all(r[5] is None and r[6] is None and r[7]=="admin" and set(r[3])<={"attempts","retry_after"} for r in audits)
        assert "sensitive-" not in str(audits) and password not in str(audits) and secret not in str(audits)
    print("PASS: independent setup limiter, concurrent single-use bootstrap, audit redaction, transactional rollback on audit failure")
finally:stop(proc)

# Real GC skips a row locked by another transaction, preserves current
# buckets, and never deletes security audit history.
with db() as c:
    for name,age,blocked in [("old-1",7200,None),("old-2",7200,7100),("recent",10,None),("live-block",7200,-300),("grace",100,None)]:
        c.execute("INSERT INTO auth_login_limits(source_key,failures,window_started,blocked_until) VALUES(%s,1,now()-%s*interval '1 second',CASE WHEN %s::integer IS NULL THEN NULL ELSE now()-%s*interval '1 second' END)",(name,age,blocked,blocked))
    audit_count=c.execute("SELECT count(*) FROM security_audit_events").fetchone()[0]
def gc():
    subprocess.run([str(exe),"gc","--limit","100"],check=True,timeout=40)
def keys():
    with db() as c:
        return {r[0] for r in c.execute("SELECT source_key FROM auth_login_limits")}
with db() as locked:
    locked.execute("SELECT source_key FROM auth_login_limits WHERE source_key='old-2' FOR UPDATE")
    gc()
    assert "old-1" not in keys() and "old-2" in keys()
gc()
assert "old-2" not in keys() and {"recent","live-block","grace"}<=keys()
with db() as c:
    assert c.execute("SELECT count(*) FROM security_audit_events").fetchone()[0]==audit_count
print("PASS: bounded expired-bucket GC, SKIP LOCKED, active/grace buckets and historical audits preserved")
print("PASS: explicit proxy trust, right-to-left chain, spoof rejection, shared versioned login buckets, fifth attempt, rolling expiry, concurrent admission and bounded Retry-After")
