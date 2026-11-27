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
def count(ip):
    with db() as c:
        row=c.execute("SELECT failures FROM auth_login_limits WHERE source_key=%s",(hashlib.sha256(ip.encode()).hexdigest(),)).fetchone()
        return row[0] if row else None
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
finally:stop(proc)
print("PASS: explicit proxy trust, right-to-left chain, spoof rejection, shared versioned login buckets, fifth attempt, rolling expiry, concurrent admission and bounded Retry-After")
