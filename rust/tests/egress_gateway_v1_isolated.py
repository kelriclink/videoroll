#!/usr/bin/env python3
"""Real Rust standalone HTTP gateway tests; mock-only destinations and no DB."""
import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

assert os.environ.get("VIDEOROLL_V2_TEST_EGRESS_LOOPBACK")=="true"
assert os.environ.get("VIDEOROLL_V2_TEST_EGRESS_DB")=="disposable"
BASE="http://127.0.0.1:18197"
UPSTREAM="http://127.0.0.1:18098"
SECRET="isolated-egress-secret-with-no-production-access"
TOKEN="v1."+hmac.new(SECRET.encode(),b"videoroll-internal-service:v1",
                     hashlib.sha256).hexdigest()
assert len(TOKEN)==67
class Mock(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=="/plain":
            value=b"the original egress gateway allowed textual payloads"
            code,mime=200,"text/plain; charset=utf-8"
        elif self.path=="/json":
            value=json.dumps({"ok":True,"cookie_seen":self.headers.get("cookie"),
                              "auth_seen":self.headers.get("authorization"),
                              "accept_language":self.headers.get("accept-language")}).encode()
            code,mime=200,"application/json"
        elif self.path=="/rate-limit":
            value=b"rate limited"
            code,mime=429,"text/plain; charset=iso-8859-1"
        elif self.path=="/slow-redirect":
            time.sleep(0.08)
            value=b""
            code,mime=302,"text/plain"
        elif self.path=="/slow-body":
            value=b"12345"
            code,mime=200,"text/plain"
        elif self.path=="/long":
            value=b"a"*5000
            code,mime=200,"text/plain"
        elif self.path=="/binary":
            value=b"\x89PNG"+b"x"*40
            code,mime=200,"image/png"
        elif self.path=="/redirect":
            value=b""
            code,mime=302,"text/plain"
        elif self.path=="/redirect-private":
            value=b""
            code,mime=302,"text/plain"
        elif self.path=="/redirect-redirect":
            value=b""
            code,mime=302,"text/plain"
        else:code,mime,value=404,"text/plain",b"not found"
        self.send_response(code)
        self.send_header("Content-Type",mime)
        if self.path=="/rate-limit":
            self.send_header("Retry-After","7")
            self.send_header("X-Evidence","one")
            self.send_header("X-Evidence","caf\xe9")
        if self.path=="/slow-redirect":self.send_header("Location","/slow-body")
        if self.path=="/redirect":self.send_header("Location","/json")
        if self.path=="/redirect-private":self.send_header("Location","http://127.0.0.2:18098/private")
        if self.path=="/redirect-redirect":self.send_header("Location","/redirect-redirect")
        self.send_header("Content-Length",str(len(value)))
        self.end_headers()
        try:
            if self.path=="/slow-body":
                for b in value:
                    time.sleep(0.06)
                    self.wfile.write(bytes([b]));self.wfile.flush()
            else:self.wfile.write(value)
        except (BrokenPipeError,ConnectionResetError):pass
    def log_message(self,*_):pass

upstream=ThreadingHTTPServer(("127.0.0.1",18098),Mock)
thread=threading.Thread(target=upstream.serve_forever,daemon=True)
thread.start()
binpath=os.path.abspath("rust/target/debug/videoroll-egress")
assert os.path.isfile(binpath),binpath
env=os.environ.copy()
env["INTERNAL_API_SECRET"]=SECRET
env["VIDEOROLL_V2_EGRESS_BIND"]="127.0.0.1:18197"
env.pop("DATABASE_URL",None) # deliberate: Gateway is a separate service
env["HTTP_PROXY"]="http://127.0.0.1:1"
env["HTTPS_PROXY"]="http://127.0.0.1:1"
process=subprocess.Popen([binpath],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
def call(payload=None,auth=TOKEN,method="POST",path="/fetch"):
    headers={"Content-Type":"application/json"}
    if auth is not None:headers["X-Videoroll-Internal-Token"]=auth
    req=Request(BASE+path,method=method,headers=headers,
                data=json.dumps(payload).encode() if payload is not None else None)
    try:response=urlopen(req,timeout=12)
    except HTTPError as exc:response=exc
    with response:
        raw=response.read()
        return response.status,json.loads(raw)
try:
    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError("Rust Egress startup exited: "+str(process.stderr.read()))
        try:
            assert call(method="GET",path="/health",auth=None)[0]==200
            break
        except OSError:time.sleep(0.15)
    else:raise AssertionError("Gateway did not start")
    assert call(None,auth=None)[0]==401
    assert call(None,auth="wrong-token")[0]==403
    assert call(None,auth=TOKEN)[0]==422
    print("PASS: standalone Rust service is DB-independent; original HMAC token auth precedes JSON parsing")

    code,body=call({"url":UPSTREAM+"/plain"})
    assert code==200,(code,body)
    assert body["status_code"]==200 and not body["truncated"]
    assert base64.b64decode(body["body_base64"]).startswith(b"the original")
    assert body["url"]==UPSTREAM+"/plain"
    assert body["headers"]["content-type"].startswith("text/plain")
    assert call({"url":UPSTREAM+"/long","max_bytes":50})[1]["truncated"] is True
    assert len(base64.b64decode(call({"url":UPSTREAM+"/long","max_bytes":50})[1]["body_base64"]))==50
    print("PASS: native GET returns Python-compatible base64 response and strictly bounded truncation")

    code,body=call({"url":UPSTREAM+"/json","headers":{
        "cookie":"must-not-send","authorization":"must-not-send","accept-language":"zh-CN"}})
    assert code==200,(code,body)
    target=json.loads(base64.b64decode(body["body_base64"]))
    assert target["cookie_seen"] is None and target["auth_seen"] is None,target
    assert target["accept_language"]=="zh-CN"
    assert call({"url":UPSTREAM+"/binary"})[0]==403
    print("PASS: strips authorization/cookies and rejects image/binary content types")

    code,body=call({"url":UPSTREAM+"/redirect"})
    assert code==200 and body["url"]==UPSTREAM+"/json",(code,body)
    assert call({"url":UPSTREAM+"/redirect","redirects":0})[0]==403
    assert call({"url":UPSTREAM+"/redirect-private"})[0]==403
    assert call({"url":UPSTREAM+"/redirect-redirect","redirects":2})[0]==403
    print("PASS: redirect-by-redirect global-IP verification, max hops and failure containment")

    code,body=call({"url":UPSTREAM+"/rate-limit"})
    assert code==200 and body["status_code"]==429
    assert body["headers"]["retry-after"]=="7"
    assert body["headers"]["x-evidence"]=="one, caf\xe9"
    started=time.monotonic()
    assert call({"url":UPSTREAM+"/slow-redirect","timeout":0.18})[0]==502
    assert time.monotonic()-started<1.0
    print("PASS: original Retry-After, repeated Latin-1 headers and a single total deadline across redirect/trickle")
    for bad in ["file:///etc/passwd","http://127.0.0.1:12345/foo",
                "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/","http://192.168.1.1/",
                "https://user:password@example.org/foo",
                "http://127.0.0.2:18098/","http://[::1]/"]:
        status,_=call({"url":bad})
        assert status==403,(bad,status)
    print("PASS: localhost except exact test fixture, IMDS, RFC1918, IPv6, custom ports and userinfo blocked")

    for bad in [{"url":UPSTREAM+"/plain","timeout":61},
                {"url":UPSTREAM+"/plain","max_bytes":2000001},
                {"url":UPSTREAM+"/plain","redirects":6}]:
        assert call(bad)[0]==422,bad
    print("PASS: Python request limits of 60 seconds, 2MB and 5 redirects")

    code,_=call({"url":UPSTREAM+"/plain","headers":{"x-bad":"one\r\ntwo"}})
    assert code in (400,403,422),code
    print("PASS: invalid header values rejected rather than sent to public origin")
finally:
    process.terminate()
    try:process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=4)
    upstream.shutdown()
