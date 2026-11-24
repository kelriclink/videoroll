#!/usr/bin/env python3
"""Check production (featureless) Egress binary cannot honor loopback test flags."""
import hashlib,hmac,json,os,subprocess,time
from urllib.request import urlopen,Request
from urllib.error import HTTPError,URLError

binary=os.path.abspath("rust/target/debug/videoroll-egress")
secret="isolated-egress-prod-guard-secret"
token="v1."+hmac.new(secret.encode(),b"videoroll-internal-service:v1",hashlib.sha256).hexdigest()
env={**os.environ,
     "INTERNAL_API_SECRET":secret,
     "VIDEOROLL_V2_EGRESS_BIND":"127.0.0.1:18197",
     "VIDEOROLL_V2_TEST_EGRESS_LOOPBACK":"true",
     "VIDEOROLL_V2_TEST_EGRESS_DB":"disposable"}
env.pop("DATABASE_URL",None)
def start(env):
    return subprocess.Popen([binary],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
for key,value in [
    ("INTERNAL_API_SECRET","videoroll-development-internal-secret"),
    ("VIDEOROLL_V2_EGRESS_BIND","0.0.0.0:18197")
]:
    bad={**env,key:value}
    bad.pop("VIDEOROLL_V2_EGRESS_ALLOW_CONTAINER_BIND",None)
    p=start(bad)
    try:
        p.wait(timeout=3)
        assert p.returncode!=0,(key,p.returncode)
    finally:
        if p.poll() is None:p.kill();p.wait(timeout=2)
print("PASS: production binary rejects default secrets and unauthorised public bind")
p=start(env)
try:
    for _ in range(30):
        try:
            r=urlopen("http://127.0.0.1:18197/health",timeout=2)
            with r: assert r.status==200
            break
        except URLError:time.sleep(.15)
    else:raise AssertionError("production Egress binary not ready")
    for url in ["http://127.0.0.1:18098/plain",
                "http://169.254.169.254/latest/meta-data/"]:
        headers={"X-Videoroll-Internal-Token":token,"Content-Type":"application/json"}
        request=Request("http://127.0.0.1:18197/fetch",headers=headers,method="POST",
                        data=json.dumps({"url":url}).encode())
        try:resp=urlopen(request,timeout=5)
        except HTTPError as exc:resp=exc
        with resp:status=resp.status
        assert status==403,(url,status)
    print("PASS: default production binary rejects local test fixture even with both test env flags")
finally:
    p.terminate()
    try:p.wait(timeout=4)
    except subprocess.TimeoutExpired:p.kill();p.wait(timeout=4)
