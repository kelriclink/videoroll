#!/usr/bin/env python3
"""One request-level, isolated contract probe per mapped Python V1 endpoint.

Not a Python-vs-Rust parity signoff.  All writes target an ephemeral test DB and
local mock sidecars.  Production endpoints, databases, cookies and API secrets
must never be provided here.
"""
import argparse
import atexit
import base64
import hashlib
import hmac
import subprocess
import time
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.cookiejar
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

p = argparse.ArgumentParser()
p.add_argument("--base", default="http://127.0.0.1:18080")
p.add_argument("--task-id", required=True)
p.add_argument("--alert-id", required=True)
p.add_argument("--out", required=True)
p.add_argument("--all-services", action="store_true", help="probe the exact original service boundaries, including HMAC")
p.add_argument("--ws-only", action="store_true", help="isolated WebSocket handshake diagnostic only")
args = p.parse_args()
if args.base not in ("http://127.0.0.1:18080", "http://127.0.0.1:18180") or (
    args.base.endswith(":18180") and not args.ws_only
):
    raise SystemExit("Refusing to probe a non-isolated API")
token = os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
root = Path(__file__).resolve().parents[1]
matrix = json.loads((root/"docs/CONTRACT_MATRIX.json").read_text())
# A standalone gateway has a different port, service HMAC and security boundary.
# Never probe /api/v1/{path} and mistake its unrelated 404 or health response
# for evidence about the dedicated Egress Gateway.
standalone = [r for r in matrix["routes"] if r["service"]=="egress_gateway"
              and r["v1_status"]!="not_native_v1"]
social = [r for r in matrix["routes"] if r["service"]=="social_publisher"
          and r["v1_status"]!="not_native_v1"]
service_health = [r for r in matrix["routes"] if r["v1_status"]!="not_native_v1"
                  and r["service"] in ("bilibili_publisher","subtitle_service","youtube_ingest")
                  and r["path"]=="/health" and r["method"]=="GET"]
mapped = [r for r in matrix["routes"] if r["v1_status"]!="not_native_v1"
          and r["service"] not in ("egress_gateway","social_publisher")
          and r not in service_health]
assert len(mapped)+len(standalone)+len(social)+len(service_health) == matrix["native_v1_partial"], "Route count and parity matrix disagree"
if args.all_services:
    assert not args.ws_only
    # Guard before ANY request or fixture mutation.
    from urllib.parse import urlsplit
    db_url = urlsplit(os.environ["DATABASE_URL"])
    assert db_url.hostname == "127.0.0.1" and db_url.path == "/videoroll_test"
    assert token == "v2-local-test-only-0123456789abcdef"
    assert os.environ["INTERNAL_API_SECRET"] == "rust-local-test-internal-secret"
    import psycopg
    embedding_root = root / "target/v2-smoke-embedding-models"
    embedding_root.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        db.execute("""INSERT INTO app_settings(key,value_json) VALUES ('subtitle.translate',jsonb_build_object('rag_embedding_model_dir',%s::text)) ON CONFLICT(key) DO UPDATE SET value_json=COALESCE(app_settings.value_json,'{}'::jsonb)||EXCLUDED.value_json""", (str(embedding_root),))
    # Preserve orchestrator coverage separately from the original standalone
    # services. A proxy success cannot stand in for a standalone response.
    mapped=[r for r in matrix["routes"] if r["v1_status"]!="not_native_v1" and r["service"]=="orchestrator_api"]
    service_rows=[r for r in matrix["routes"] if r["v1_status"]!="not_native_v1" and r["service"]!="orchestrator_api"]
else:
    service_rows=[]

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
anonymous = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
fake = "00000000-0000-0000-0000-000000000011"
context = {"task_id": args.task_id,"id":args.task_id,"alert_id":args.alert_id,
           "source_pk": fake, "source_type": "channel",
           "asset_id": fake,"job_id":fake,"publish_job_id":fake,
           "account_id":fake,"session_id":fake,"platform":"bilibili"}
context["provider_id"]="isolated-provider-diff-probe"
context["service_path:path"]="not-an-allowed-operation"
def send(method, path, payload=None, authenticated=True, form=False, headers=None, use_cookie=False):
    if not path.startswith("/api/") or not args.base.startswith("http://127.0.0.1:"):
        raise RuntimeError("unsafe request destination")
    h = dict(headers or {})
    if authenticated and not use_cookie:
        h["x-videoroll-v2-internal-token"] = token
    if payload is None:
        data = None
    elif form:
        data = payload
        h["content-type"] = "multipart/form-data; boundary=vr2contract"
    else:
        data = json.dumps(payload).encode()
        h["content-type"] = "application/json"
    req = urllib.request.Request(args.base+path, data=data, headers=h, method=method)
    try:
        res = (opener if use_cookie else anonymous).open(req, timeout=6)
    except urllib.error.HTTPError as exc:
        res = exc
    with res:
        raw = res.read(8192)
        mime = res.headers.get("Content-Type","").split(";")[0]
        status = res.status
        obj = None
        if mime=="application/json":
            try: obj=json.loads(raw)
            except Exception: pass
        return status, mime, obj

def websocket_probe(authenticated=True, origin=None):
    """Actually upgrade a local WebSocket and verify its first application frame.

    The original WS API does not accept normal HTTP GET or an admin bearer token:
    auth is from a browser cookie and the Origin must match the Host.
    """
    host, port = "127.0.0.1", int(args.base.rsplit(":",1)[1])
    if port not in (18080,18180) or args.base != f"http://{host}:{port}":
        raise RuntimeError("WebSocket probe requires the isolated server")
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request_origin = origin or args.base
    headers = [
        "GET /api/v1/ws/events HTTP/1.1",
        f"Host: {host}:{port}",
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {key}",
        f"Origin: {request_origin}",
    ]
    if authenticated:
        cookies = [f"{cookie.name}={cookie.value}" for cookie in jar
                   if cookie.name == "videoroll_admin_device"]
        if not cookies:
            raise RuntimeError("no isolated admin cookie for WebSocket probe")
        headers.append(f"Cookie: {'; '.join(cookies)}")
    request = ("\r\n".join(headers)+"\r\n\r\n").encode()
    with socket.create_connection((host,port), timeout=6) as conn:
        conn.settimeout(6)
        conn.sendall(request)
        pending=b""
        while b"\r\n\r\n" not in pending:
            part=conn.recv(4096)
            if not part or len(pending)+len(part)>16384:
                raise RuntimeError("invalid WebSocket upgrade header")
            pending+=part
        head,pending=pending.split(b"\r\n\r\n",1)
        lines=head.decode("iso-8859-1").split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101 "):
            raise RuntimeError(f"expected WebSocket HTTP 101: {lines[0][:120]}")
        expected=base64.b64encode(hashlib.sha1(
            (key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        response_headers={}
        for line in lines[1:]:
            if ":" in line:
                k,v=line.split(":",1)
                response_headers[k.lower().strip()]=v.strip()
        if response_headers.get("sec-websocket-accept")!=expected:
            raise RuntimeError("invalid WebSocket challenge response")

        def take(size):
            nonlocal pending
            while len(pending)<size:
                chunk=conn.recv(4096)
                if not chunk:
                    raise RuntimeError("WebSocket closed before first frame")
                pending+=chunk
            got,pending=pending[:size],pending[size:]
            return got
        first,second=take(2)
        opcode=first&0x0f
        size=second&0x7f
        if second&0x80:
            raise RuntimeError("server WebSocket frames must not be masked")
        if size==126:
            size=struct.unpack("!H",take(2))[0]
        elif size==127:
            size=struct.unpack("!Q",take(8))[0]
        if size>16384:
            raise RuntimeError("WebSocket initial frame is too large")
        frame=take(size)
        if authenticated and request_origin==args.base:
            if opcode!=1 or json.loads(frame).get("type")!="ready":
                raise RuntimeError("authenticated WebSocket did not become ready")
            return 101
        if opcode!=8 or len(frame)<2:
            raise RuntimeError("denied WebSocket did not close")
        return struct.unpack("!H",frame[:2])[0]


class EmbeddingProbeBackend(BaseHTTPRequestHandler):
    """Disposable, loopback-only OpenAI embedding endpoint for a positive probe."""
    def log_message(self,*_):
        pass
    def do_POST(self):
        try:
            n=int(self.headers.get("content-length","0"))
            if self.path!="/v1/embeddings" or n<1 or n>8192:
                self.send_error(400)
                return
            request=json.loads(self.rfile.read(n))
            if self.headers.get("Authorization")!="Bearer isolated-embedding-test-key" or (
                request.get("model")!="isolated-embedding-model"
                or request.get("dimensions")!=3
                or request.get("input")!="Contract probe only"
            ):
                self.send_error(400)
                return
            result=json.dumps({"data":[{"index":0,"embedding":[0.25,0.5,0.75]}]}).encode()
            self.send_response(200)
            self.send_header("content-type","application/json")
            self.send_header("content-length",str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        except (ValueError,KeyError,TypeError):
            self.send_error(400)


def first_id(o):
    if isinstance(o, list):
        for v in o:
            x=first_id(v)
            if x:return x
    elif isinstance(o, dict):
        for key in ("items", "jobs", "assets", "records", "results"):
            if key in o:
                x=first_id(o[key])
                if x:return x
        val=o.get("id")
        if isinstance(val,str):
            try: uuid.UUID(val); return val
            except ValueError: pass
    return None

# Populate the only IDs we need from an existing disposable DB fixture.
for suffix, key in [("assets","asset_id"),("dubbing_jobs","job_id"),
                    ("publish_jobs","publish_job_id")]:
    try:
        status,_,obj=send("GET",f"/api/v1/tasks/{args.task_id}/{suffix}")
        if status==200:
            context[key]=first_id(obj) or fake
    except Exception:
        pass

# Validate cookie-backed login/logout independently of the internal token.
cookie_status = None
try:
    cookie_status,_,_=send("POST","/api/auth/login",{"password":"rust-test-secret-123"},
                           authenticated=False,use_cookie=True)
except Exception: pass

if args.ws_only:
    print("WebSocket cookie count",sum(c.name=="videoroll_admin_device" for c in jar))
    for valid,origin in [(True,None),(False,None),(True,"http://evil.example")]:
        print("WebSocket probe",valid,origin,"result",websocket_probe(valid,origin))
    print("PASS isolated WebSocket handshake, authenticated ready and rejected origins")
    raise SystemExit(0)

read_only = {"/health","/auth/status","/operations/alerts",
 "/operations/ai-usage","/operations/ai-usage/pricing","/settings/storage",
 "/settings/dubbing/jobs/summary","/render-management/connection",
 "/render-management/enrollments","/render-management/workers",
 "/render-management/executions","/render-management/executions/{execution_id}",
 "/subtitle/auto/profile","/subtitle/jobs/{job_id}",
 "/subtitle/asr/settings",
 "/subtitle/asr/external/test", "/subtitle/asr/groq/test", "/subtitle/asr/cloudflare/test",
 "/subtitle/dictionaries/sources", "/subtitle/dictionaries/sources/{source_id}",
 "/subtitle/dictionaries/entries", "/subtitle/dictionaries/entries/{entry_id}",
 "/subtitle/dictionaries/lookup", "/subtitle/dictionaries/import-presets",
 "/subtitle/knowledge/items",
 "/subtitle/embedding/models", "/subtitle/embedding/models/list",
 "/subtitle/embedding/runtime", "/subtitle/models","/subtitle/models/{name}",
 "/subtitle/models/proxy/test",
 "/subtitle/models/upload",
 "/subtitle/models/download",
 "/subtitle/task_queue","/subtitle/task_queue/broker",
 "/subtitle/translate/settings",
 "/subtitle/tasks/{task_id}/quality","/subtitle/tasks/{task_id}/translation-context",
 "/subtitle/translate/model-providers",
 "/subtitle/translate/model-providers/{provider_id}",
 "/subtitle/translate/model-providers/{provider_id}/remote-models",
 "/settings/publish/platforms","/settings/publish/social/accounts",
 "/youtube/sources",
 "/settings/review","/settings/youtube","/system/resources",
 "/tasks","/tasks/{task_id}","/tasks/{task_id}/assets",
 "/tasks/{task_id}/dubbing_jobs","/tasks/{task_id}/playout-assets",
 "/tasks/{task_id}/publish_jobs","/tasks/{task_id}/publish_meta",
 "/tasks/{task_id}/publish_meta/draft","/tasks/{task_id}/publish_review",
 "/tasks/{task_id}/subtitle_jobs","/tasks/{task_id}/youtube_download_progress",
 "/tasks/{task_id}/youtube_meta","/videos/converted"}
public = {"/auth/login","/auth/logout","/auth/setup","/auth/status","/health"}
public.update({"/render-workers/v1/protocol","/render-workers/v1/enroll"})
# A local purpose-built embedding sidecar, independent of any production API.
embedding_backend = ThreadingHTTPServer(("127.0.0.1",0),EmbeddingProbeBackend)
threading.Thread(target=embedding_backend.serve_forever,daemon=True).start()
atexit.register(embedding_backend.server_close)
atexit.register(embedding_backend.shutdown)

# Safe test payloads: all side effects are in disposable DB / localhost mocks.
bodies = {
 "/subtitle/embedding/test":{"text":"Contract probe only","provider":"openai",
     "model":"isolated-embedding-model","dimensions":3,
     "api_key":"isolated-embedding-test-key",
     "base_url":f"http://127.0.0.1:{embedding_backend.server_port}/v1"},
 "/auth/login":{"password":"rust-test-secret-123"},
 "/auth/setup":{"password":"rust-test-secret-123"},
 "/auto/youtube":{"url":"https://youtu.be/CONTRA00001","source_license":"authorized"},
 "/youtube/ingest":{"url":"https://youtu.be/CONTRA00002","source_license":"authorized"},
 "/youtube/sources":{"source_type":"channel","source_id":"UC12345678901234567890","license":"authorized"},
 "/youtube/scan":{"source_type":"channel","source_id":"UC12345678901234567890","limit":1,"auto_process":False},
 "/youtube/sources/{source_pk}/scan":{"limit":1,"auto_process":False},
 "/youtube/sources/{source_pk}/scan/start":{"limit":1,"auto_process":False},
 "/settings/review":{},
 "/settings/youtube":{},
 "/settings/storage":{},
 "/operations/ai-usage/pricing":{"models":{}},
 "/render-management/connection":{"server_url":"https://render-compat.example.test"},
 "/subtitle/auto/profile":{},
 "/subtitle/asr/settings":{"default_language":"zh","default_engine":"faster-whisper"},
 "/subtitle/translate/settings":{"default_target_lang":"zh","index_max_tokens":4096},
 # Generic contract scan validates input rejection without relying on a model
 # whose settings may change throughout this fixture. Positive OpenAI and Index
 # HTTP calls are covered by isolated-bili-account.sh with a loopback mock.
 "/subtitle/translate/test":{"text":""},
 "/subtitle/embedding/models/list":{},
 "/subtitle/embedding/models/download":{"model":"BAAI/bge-small-zh-v1.5","name":"mock-embedding-test"},
 "/subtitle/dictionaries/promote":{"entry_id":"00000000-0000-0000-0000-000000010382"},
 "/subtitle/knowledge/items":{"item_type":"term","term":"isolated probe term","translation":"隔离测试"},
 "/subtitle/knowledge/rebuild-embeddings":{"item_type":"document","limit":1},

 "/subtitle/tasks/{task_id}/translation-context":{"summary":"isolated","memory":{"topic":"demo"}},
 "/subtitle/task_queue/settings":{"max_concurrent":3},
 "/subtitle/translate/model-providers":{"name":"Isolated probe","base_url":"http://127.0.0.1:18082/v1","models":[{"id":"local-model"}]},
 "/subtitle/translate/model-providers/{provider_id}":{"name":"Isolated updated","base_url":"http://127.0.0.1:18082/v1","models":[{"id":"local-model"}]},
 "/render-management/enrollments":{"label":"isolated-contract-probe","ttl_minutes":5},
 "/settings/youtube/test":{"url":"http://127.0.0.1/admin"},
 "/settings/publish/platforms/{platform}":{"enabled":True},
 "/tasks":{"source_type":"youtube","source_url":"https://youtu.be/CONTRA00003","source_license":"authorized"},
 "/tasks/{task_id}/actions/auto_publish":{},
 "/tasks/{task_id}/actions/auto_subtitle_handoff":{},
 "/tasks/{task_id}/actions/auto_youtube_start":{},
 "/tasks/{task_id}/actions/dubbing":{},
 "/tasks/{task_id}/actions/publish":{},
 "/tasks/{task_id}/actions/publish_all":{"platforms":[]},
 "/tasks/{task_id}/actions/publish_review":{},
 "/tasks/{task_id}/actions/resume":{},
 "/tasks/{task_id}/actions/stop":{},
 "/tasks/{task_id}/actions/subtitle":{},
 "/tasks/{task_id}/actions/subtitle_resume":{},
 "/tasks/{task_id}/actions/youtube_meta":{},
 "/tasks/{task_id}/dubbing_jobs/{job_id}/control":{"action":"invalid"},
 "/tasks/{task_id}/dubbing_jobs/{job_id}/retry":{},
 "/tasks/{task_id}/publish_jobs/{publish_job_id}/append_p2":{},
 "/tasks/{task_id}/publish_meta":{},
 "/tasks/{task_id}/publish_meta/draft":{},
 "/operations/alerts/report":{"fingerprint":"vr2.contract.64","title":"Contract probe",
  "source":"isolated","severity":"warning","message":"isolated only"},
 "/operations/alerts/scan":{},
 "/operations/alerts/{alert_id}/ack":{},
 "/operations/alerts/{alert_id}/resolve":{},
 "/settings/publish/social/login-sessions/{platform}":{"account_name":"ContractProbe"},
 "/settings/publish/social/accounts/{account_id}/check":{},
 "/tasks/actions/stop_all":{},
 "/tasks/actions/resume_stopped":{},
 "/tasks/actions/resume_failed_recent":{},
}
# Give positive reads and lifecycle operations their own stable fixtures rather
# than accidentally probing a nonexistent ID or a task mutated by another test.
fixture_ids={}
if args.all_services:
    meta_task,yt_task,render_task,render_job,execution,worker,enrollment=[str(uuid.uuid4()) for _ in range(7)]
    metadata={"title":"Contract fixture metadata","description":"Isolated fixture","webpage_url":"https://youtu.be/0123456789A","uploader":"Contract fixture","duration":23}
    storage=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
    assert storage==root/"target/v2-smoke-objects"
    key=f"v2/metadata/{yt_task}/contract.json"
    file=storage/key;file.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(metadata).encode();file.write_bytes(raw)
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        db.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','ready_for_review','rust'),(%s,'local','own','subtitle_ready','rust')",(meta_task,render_task))
        db.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,source_url) VALUES(%s,'youtube','authorized','created','rust',%s)",(yt_task,metadata['webpage_url']))
        db.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'metadata_json',%s,%s,%s)",(uuid.uuid4(),yt_task,key,hashlib.sha256(raw).hexdigest(),len(raw)))
        db.execute("INSERT INTO render_workers(id,worker_key,name,platform,resources,credential_hash) VALUES(%s,%s,'contract render worker','linux',%s,%s)",(worker,'contract-'+worker,json.dumps({'devices':[{'id':'gpu0','name':'fixture','status':'idle'}]}),'f'*64))
        db.execute("INSERT INTO render_worker_enrollments(id,label,status,expires_at) VALUES(%s,'contract fixture','active',now()+interval '10 minutes')",(enrollment,))
        db.execute("INSERT INTO render_jobs(id,task_id,status) VALUES(%s,%s,'running')",(render_job,render_task))
        db.execute("INSERT INTO render_executions(id,render_job_id,worker_id,state,progress,metrics) VALUES(%s,%s,%s,'running',47,'{\"fps\":18.5}')",(execution,render_job,worker))
    fixture_ids.update(meta_task=meta_task,yt_task=yt_task,execution=execution,worker=worker,enrollment=enrollment)
    context.update(execution_id=execution,worker_id=worker,enrollment_id=enrollment,**{"device_id:path":"gpu0"})
    bodies["/tasks/{task_id}/publish_meta"]={"title":"Contract fixture title","typeid":21,"tags":["fixture"],"copyright":1}
    bodies["/tasks/{task_id}/publish_meta/draft"]={"mode":"source","meta":{"typeid":21}}
    bodies["/render-management/workers/{worker_id}"]={"max_concurrency":3,"draining":True}
    bodies["/render-management/workers/{worker_id}/devices/{device_id:path}"]={"enabled":False}
    # Use the proxy's real session response before probing GET/DELETE. The
    # sidecar is the explicit loopback mock, not an actual browser/account.
    assert os.environ["VIDEOROLL_V2_SOCIAL_PUBLISHER_URL"]=="http://127.0.0.1:18085"
    code,_,session=send("POST","/api/v1/settings/publish/social/login-sessions/douyin",{"account_name":"ContractProbe"})
    assert code==200 and first_id(session), (code,session)
    context["session_id"]=first_id(session)
    code,_,saved=send("PUT",f"/api/v1/tasks/{meta_task}/publish_meta",bodies["/tasks/{task_id}/publish_meta"])
    assert code==200 and saved.get("stored") and saved["meta"]["title"]=="Contract fixture title", (code,saved)
# Do not interfere with other probes; global worker stop/resume actions run last.
late = {"/tasks/actions/stop_all","/tasks/actions/resume_stopped",
        "/tasks/actions/resume_failed_recent","/tasks/{task_id}/actions/stop",
        "/tasks/{task_id}/actions/resume"}
# Run WebSocket before the logout route revokes the fixture's admin cookie.
# Socket auth is cookie-only; an internal HTTP token cannot substitute for it.
ordered = sorted(mapped, key=lambda r: (
    r["method"] != "WEBSOCKET", r["path"] in late, r["method"] != "GET"))
results = []
if args.all_services:
    from contract_media_v3 import probe
    results.extend(probe(args.base,token))
    from contract_render_worker_v4 import probe as worker_probe
    results.extend(worker_probe(args.base,token))
    from contract_orchestrator_v6 import probe as orchestrator_probe
    results.extend(orchestrator_probe(root,send))
    from contract_actions_v7 import probe as action_probe
    results.extend(action_probe(root,send))
covered={(r["method"],r["path"]) for r in results}
for row in ordered:
    if (row["method"],row["path"]) in covered:continue
    template=row["path"]
    if row["method"]=="WEBSOCKET":
        try:
            status=websocket_probe(authenticated=True)
            unauthorized_close=websocket_probe(authenticated=False)
            bad_origin_close=websocket_probe(authenticated=True,
                origin="http://evil.example")
            if unauthorized_close!=4401 or bad_origin_close!=4403:
                raise RuntimeError("WebSocket authorization/origin close codes mismatch")
            record={"method":row["method"],"path":template,"http_status":status,
                "response_kind":"websocket","probe":"positive","issue":None,
                "unauthorized_status":unauthorized_close,
                "origin_denied_close":bad_origin_close,
                "json_fields":["type","heartbeat_seconds"]}
        except Exception as exc:
            record={"method":row["method"],"path":template,"http_status":None,
                "probe":"failed","issue":"websocket_probe_"+type(exc).__name__+":"+str(exc)[:130]}
        results.append(record)
        continue
    path=template
    probe_context=dict(context)
    if fixture_ids and template in ("/tasks/{task_id}/publish_meta","/tasks/{task_id}/publish_meta/draft"):
        probe_context["task_id"]=fixture_ids["meta_task"]
    elif fixture_ids and template=="/tasks/{task_id}/youtube_meta":
        probe_context["task_id"]=fixture_ids["yt_task"]
    if template=="/settings/publish/social/login-sessions/{platform}":probe_context["platform"]="douyin"
    for field in re.findall(r"\{([^{}]+)\}",path):
        path=path.replace("{"+field+"}",probe_context.get(field, fake))
    if template=="/settings/publish/social/accounts":
        path += "?platform=douyin"
    if template=="/settings/publish/platforms/{platform}":
        path=path.replace("/bilibili","/bilibili")
    payload=bodies.get(template)
    if row["method"] in ("GET","DELETE"):
        payload=None
    if row["method"] in ("POST","PUT","PATCH") and payload is None:
        payload={}
    form=False
    if template=="/settings/publish/social/accounts/{platform}":
        path=path.replace("/bilibili","/douyin")
        payload=b"--vr2contract\r\nContent-Disposition: form-data; name=\"account_name\"\r\n\r\nTest\r\n--vr2contract--\r\n"
        form=True
    if template in ("/tasks/{task_id}/upload/video","/tasks/{task_id}/upload/cover"):
        # Deliberately malformed multipart: must reject without storing media.
        payload=b"invalid multipart"
        form=True
    with_cookie = template in ("/auth/login", "/auth/logout")
    # Windows Worker V1 is mounted at /api/render-workers/v1, not /api/v1/...
    # Its protected calls require a vrw_ Bearer, not the admin session token.
    worker_wire=template.startswith("/render-workers/v1/")
    # Python's retired GET query-token endpoint is intentionally public 410.
    # Its POST replacement is NOT public: use the independent Bearer secret.
    public_wire=template in public or (template=="/remote/auto/youtube" and row["method"]=="GET")
    actual_path=("/api" if worker_wire else "/api/v1")+path
    if template=="/render-workers/v1/executions/{execution_id}/output":
        actual_path += "?fence_token=invalid-probe"
    try:
        status,mime,obj=send(row["method"],actual_path,payload,
                              authenticated=not public_wire,
                              form=form,use_cookie=with_cookie,
                              headers={"Range":"bytes=0-15"} if template.endswith("/stream") else None)
        issue=None
        if status>=500 or status in (301,302,303,307,308,405) or (status==401 and template not in public and not worker_wire
            and template not in ("/desktop/grants","/desktop/authorize")):
            issue="server_failure_or_unexpected_auth"
        if status==404 and template in read_only:
            issue="existing_fixture_read_returned_404"
        if template in ("/health", "/auth/status") and status!=200:
            issue="public_health_or_auth_status_incompatible"
        if template=="/tasks/{task_id}/actions/publish" and status==502:
            issue="native_bilibili_disabled_in_isolated_test"
        category="positive" if 200<=status<300 else "blocked" if issue=="native_bilibili_disabled_in_isolated_test" else "rejection" if 400<=status<500 else "failed"
        record={"method":row["method"],"path":template,"http_status":status,
                "response_kind":mime,"probe":category,"issue":issue,
                "json_fields":sorted(obj.keys())[:22] if isinstance(obj,dict) else []}
        if fixture_ids:
            checked=None
            if template=="/render-management/executions/{execution_id}" and row["method"]=="GET":
                checked=status==200 and isinstance(obj,dict) and obj.get("id")==fixture_ids["execution"] and obj.get("progress")==47 and obj.get("metrics")=={"fps":18.5}
            elif template=="/tasks/{task_id}/publish_meta":
                if row["method"]=="GET":checked=status==200 and isinstance(obj,dict) and obj.get("title")=="Contract fixture title"
                elif row["method"]=="PUT":checked=status==200 and isinstance(obj,dict) and obj.get("stored") is True and obj.get("meta",{}).get("title")=="Contract fixture title"
            elif template=="/tasks/{task_id}/youtube_meta" and row["method"]=="GET":
                checked=status==200 and isinstance(obj,dict) and obj.get("title")=="Contract fixture metadata" and obj.get("duration")==23
            elif template=="/settings/publish/social/login-sessions/{session_id}":
                checked=status==200 and isinstance(obj,dict) and obj.get("id")==context["session_id"] and obj.get("state")==("canceled" if row["method"]=="DELETE" else "pending")
            if checked is not None:
                record["fixture_asserted"]=bool(checked)
                if not checked:record.update(probe="failed",issue="positive_fixture_assertion_failed")
        if template=="/settings/publish/social/login-sessions/{platform}" and isinstance(obj,dict):
            context["session_id"]=first_id(obj) or fake
        if template=="/operations/alerts/report" and isinstance(obj,dict):
            context["alert_id"]=first_id(obj) or context["alert_id"]
    except Exception as exc:
        record={"method":row["method"],"path":template,"http_status":None,
                "probe":"failed","issue":type(exc).__name__}
    # Check unauthorized request for each protected endpoint without mutating.
    if not public_wire and not worker_wire:
        try:
            unauth,_,_=send(row["method"],actual_path,None,authenticated=False)
            record["unauthorized_status"]=unauth
            if unauth not in (401,403):
                record["issue"]=record["issue"] or "unauthenticated_request_not_rejected"
        except Exception as exc:
            record["issue"]=record["issue"] or "unauthorized_check_"+type(exc).__name__
    results.append(record)

assert {(r["method"],r["path"]) for r in results} == {(r["method"],r["path"]) for r in mapped}
# Label each record by original source service (old probe mode may include
# non-orchestrator routes on its compatibility gateway).
service_map={(r["method"],r["path"]):r["service"] for r in mapped}
for record in results:record["service"]=service_map[(record["method"],record["path"])]
if args.all_services:
    assert os.environ["DATABASE_URL"].endswith("/videoroll_test") and "127.0.0.1:" in os.environ["DATABASE_URL"]
    assert token=="v2-local-test-only-0123456789abcdef"
    secret=os.environ["INTERNAL_API_SECRET"]
    assert secret=="rust-local-test-internal-secret"
    service_token="v1."+hmac.new(secret.encode(),b"videoroll-internal-service:v1",hashlib.sha256).hexdigest()
    binaries=root/"target/debug"
    specifications={
       "bilibili_publisher":("videoroll-v2",["service","bilibili-publisher"],"VIDEOROLL_V2_BIND"),
       "subtitle_service":("videoroll-v2",["service","subtitle-service"],"VIDEOROLL_V2_BIND"),
       "youtube_ingest":("videoroll-v2",["service","youtube-ingest"],"VIDEOROLL_V2_BIND"),
       "social_publisher":("videoroll-sau",[],"VIDEOROLL_V2_SAU_BIND"),
       "egress_gateway":("videoroll-egress",[],"VIDEOROLL_V2_EGRESS_BIND"),
    }
    def service_call(port,method,path,payload=None,auth=True,form=False):
        headers={}
        if auth:headers["X-Videoroll-Internal-Token"]=service_token
        if form:
            headers["content-type"]="multipart/form-data; boundary=vr2contract"
            data=payload
        elif payload is None:data=None
        else:
            headers["content-type"]="application/json"
            data=json.dumps(payload).encode()
        req=urllib.request.Request(f"http://127.0.0.1:{port}"+path,data=data,method=method,headers=headers)
        try:res=urllib.request.urlopen(req,timeout=12)
        except urllib.error.HTTPError as e:res=e
        with res:
            raw=res.read(1024*1024)
            try:obj=json.loads(raw)
            except ValueError:obj=None
            return res.status,res.headers.get("Content-Type","").split(";")[0],obj
    from contract_services_v5 import Fixtures
    service_fixtures=Fixtures(root)
    atexit.register(service_fixtures.close)
    for service,(binary,cmd,bindvar) in specifications.items():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            port=sock.getsockname()[1]
        # No real platform jobs or side effects. SAU mutating browser routes
        # deliberately remain disabled in this broad request-level probe.
        env={**os.environ,bindvar:f"127.0.0.1:{port}",
             "VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK":"false",
             "VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN":"false",
             "VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH":"false",
             "VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX":"false"}
        service_fixtures.configure(service,env)
        sau_fixtures=[]
        if service=="social_publisher":
            from contract_sau_v8 import probe as sau_probe
            sau_fixtures=sau_probe(root,env,port,binaries/binary,lambda method,path,body=None,auth=True,form=False:service_call(port,method,path,body,auth,form))
        proc=subprocess.Popen([str(binaries/binary),*cmd],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                assert proc.poll() is None,(service,"service exited")
                try:
                    if service_call(port,"GET","/health",auth=False)[0]==200:break
                except (OSError,urllib.error.URLError):pass
                time.sleep(.15)
            else:raise RuntimeError(f"{service} health timeout")
            positive_fixtures=service_fixtures.probe(service,lambda method,path,body=None,auth=True,form=False:service_call(port,method,path,body,auth,form))
            positive_fixtures.extend(sau_fixtures)
            results.extend(positive_fixtures)
            fixture_routes={(r['method'],r['path']) for r in positive_fixtures}
            # Use real IDs returned by create routes, and run deletion last.
            provider_base="/subtitle/translate/model-providers"
            source_base="/youtube/sources"
            knowledge_base="/subtitle/knowledge/items"
            service_context={**context,"platform":"douyin","account_id":fake,"session_id":fake}
            # Each original service gets its own persisted, disposable fixtures.
            with psycopg.connect(os.environ["DATABASE_URL"]) as db:
                if service=="subtitle_service":
                    task_id,job_id,run_id,source_id,entry_id=[str(uuid.uuid4()) for _ in range(5)]
                    db.execute("INSERT INTO tasks(id,source_type,source_license,status) VALUES (%s,'local','own','subtitle_ready')",(task_id,))
                    db.execute("INSERT INTO subtitle_jobs(id,task_id,status,progress) VALUES (%s,%s,'completed',100)",(job_id,task_id))
                    db.execute("INSERT INTO translation_agent_runs(id,task_id,subtitle_job_id,status,term) VALUES (%s,%s,%s,'completed','contract term')",(run_id,task_id,job_id))
                    db.execute("INSERT INTO translation_dictionary_sources(id,name,slug,source_lang,target_lang,entry_count) VALUES (%s,'Contract dictionary',%s,'en','zh',1)",(source_id,'contract-'+source_id))
                    db.execute("INSERT INTO translation_dictionary_entries(id,source_id,source_lang,target_lang,term,normalized_term,translations,translation_text) VALUES (%s,%s,'en','zh','contract term','contract term','[\"测试术语\"]'::jsonb,'测试术语')",(entry_id,source_id))
                    service_context.update(task_id=task_id,job_id=job_id,run_id=run_id,source_id=source_id,entry_id=entry_id)
                    # Separate native subtitle creation task with an actual source file.
                    create_task=str(uuid.uuid4());media_key=f"v2/contract-subtitle/{create_task}/input.mp4"
                    media=(root/"target/contract-v3-media.mp4").read_bytes()
                    target=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])/media_key
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(media)
                    db.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','ingested','rust')",(create_task,))
                    db.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw',%s,%s,%s)",(uuid.uuid4(),create_task,media_key,hashlib.sha256(media).hexdigest(),len(media)))
                    service_context.update(create_task=create_task,media_key=media_key,import_slug="contract-v4-"+uuid.uuid4().hex[:12],knowledge_term="contract knowledge "+uuid.uuid4().hex[:12])
                elif service=="bilibili_publisher":
                    task_id,job_id=[str(uuid.uuid4()) for _ in range(2)]
                    db.execute("INSERT INTO tasks(id,source_type,source_license,status) VALUES (%s,'local','own','ready_for_review')",(task_id,))
                    db.execute("INSERT INTO publish_jobs(id,task_id,platform,state) VALUES (%s,%s,'bilibili','draft')",(job_id,task_id))
                    service_context.update(task_id=task_id,job_id=job_id)
                elif service=="social_publisher":
                    account_id=str(uuid.uuid4())
                    db.execute("INSERT INTO accounts(id,platform,name,check_state) VALUES (%s,'douyin',%s,'unchecked')",(account_id,'contract-'+account_id))
                    service_context.update(account_id=account_id)
                    # Exercise persisted session read/cancel with browser login
                    # deliberately disabled; this never starts a real browser.
                    session_id=str(uuid.uuid4())
                    db.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,request_json,result_json) VALUES(%s,%s,'rust-sau-login','pending',%s,%s)",(session_id,'sau-login:'+session_id,json.dumps({'platform':'douyin','account_name':'Contract session','browser_url':'http://127.0.0.1:8090/session'}),json.dumps({'state':'starting','message':None})))
                    service_context.update(session_id=session_id)
            if service=="subtitle_service":
                # A named disposable model for each delete endpoint, never the deployed model directory.
                model_name="contract-model-"+uuid.uuid4().hex[:12]
                whisper_root=Path(os.environ["SUBTITLE_WHISPER_MODEL_DIR"])
                assert whisper_root == root / "target/v2-smoke-whisper-models"
                for directory in (whisper_root,embedding_root):
                    (directory/model_name).mkdir(parents=True,exist_ok=False)
                    (directory/model_name/"config.json").write_text("{}")
                service_context["name"]=model_name
            lifecycle_order={
                ("POST",provider_base):0,
                ("GET",provider_base+"/{provider_id}/remote-models"):1,
                ("PUT",provider_base+"/{provider_id}"):2,
                ("DELETE",provider_base+"/{provider_id}"):3,
                ("POST",source_base):0,
                ("PATCH",source_base+"/{source_pk}"):1,
                ("POST",source_base+"/{source_pk}/scan"):2,
                ("POST",source_base+"/{source_pk}/scan/start"):3,
                ("DELETE",source_base+"/{source_pk}"):4,
                ("POST",knowledge_base):0,
                ("DELETE",knowledge_base+"/{item_id}"):4,
                ("POST","/subtitle/dictionaries/lookup"):0,
                ("POST","/subtitle/dictionaries/promote"):1,
                ("PUT","/subtitle/dictionaries/entries/{entry_id}"):2,
                ("PUT","/subtitle/dictionaries/sources/{source_id}"):2,
                ("DELETE","/subtitle/dictionaries/entries/{entry_id}"):3,
                ("DELETE","/subtitle/dictionaries/sources/{source_id}"):4,
                ("DELETE","/accounts/{account_id}"):4,
                ("GET","/login-sessions/{session_id}"):0,
                ("DELETE","/login-sessions/{session_id}"):4,
            }
            service_order=sorted((r for r in service_rows if r["service"]==service),
                key=lambda r:lifecycle_order.get((r["method"],r["path"]),1))
            for row in service_order:
                template=row["path"];method=row["method"];path=template
                if (method,template) in fixture_routes:continue
                ctx=service_context
                for field in re.findall(r"\{([^{}]+)\}",path):
                    path=path.replace("{"+field+"}",ctx.get(field,fake))
                body=None if method in ("GET","DELETE") else bodies.get(template,{})
                if template==provider_base and method=="POST":
                    body={"name":"Contract catalog fixture","base_url":"http://127.0.0.1:18088/v1","api_key":"model-test-key","models":[{"id":"model-z"}]}
                elif template==provider_base+"/{provider_id}" and method=="PUT":
                    body={"name":"Contract catalog updated","base_url":"http://127.0.0.1:18088/v1","models":[{"id":"model-b"}]}
                elif template==source_base and method=="POST":
                    body={"source_type":"channel","source_id":"UC12345678901234567890","license":"authorized","auto_process":False}
                elif template==source_base+"/{source_pk}" and method=="PATCH":
                    body={"display_name":"Contract source","scan_limit":2,"auto_process":False}
                elif template in (source_base+"/{source_pk}/scan",source_base+"/{source_pk}/scan/start"):
                    body={"limit":2,"auto_process":False}
                if template=="/subtitle/dictionaries/promote":
                    body={"entry_id":ctx["entry_id"]}
                elif template=="/subtitle/dictionaries/entries/{entry_id}" and method=="PUT":
                    body={"enabled":True}
                elif template=="/subtitle/dictionaries/sources/{source_id}" and method=="PUT":
                    body={"description":"updated contract fixture"}
                elif template=="/subtitle/embedding/models/download":
                    body={"model":"BAAI/bge-small-zh-v1.5","name":"contract-download-"+uuid.uuid4().hex[:12],"model_dir":str(embedding_root)}
                elif template=="/subtitle/embedding/models/list":
                    body={"model_dir":str(embedding_root)}
                if service=="egress_gateway" and template=="/fetch":
                    # A private target must be rejected by the SSRF guard;
                    # never access a real outside resource in a broad probe.
                    body={"url":"http://127.0.0.1:1/"}
                if template=="/accounts":path+="?platform=douyin"
                form=template in ("/accounts/{platform}","/subtitle/models/upload")
                if form:body=b"--vr2contract\r\nContent-Disposition: form-data; name=\"account_name\"\r\n\r\nTest\r\n--vr2contract--\r\n"
                if service=="subtitle_service":
                    def multipart_fixture(fields,filename,data):
                        raw=b""
                        for key,value in fields.items():
                            raw+=f'--vr2contract\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                        return raw+f'--vr2contract\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+data+b"\r\n--vr2contract--\r\n"
                    if template=="/subtitle/jobs":
                        body={"task_id":ctx["create_task"],"input":{"type":"storage","key":ctx["media_key"]},"prefer_youtube_subtitles":False,"youtube_subtitle_mode":"off","translate":{"enabled":False}}
                    elif template=="/subtitle/dictionaries/lookup":
                        body={"term":"contract term","source_lang":"en","target_lang":"zh","exact":True}
                    elif template=="/subtitle/dictionaries/import":
                        form=True;body=multipart_fixture({"slug":ctx["import_slug"],"name":"Contract v4 import","format_name":"csv","source_lang":"en","target_lang":"zh","import_mode":"upsert"},"contract.csv","term,translation\nfixture trace,测试走线\n".encode())
                    elif template=="/subtitle/knowledge/import":
                        form=True;body=multipart_fixture({"import_format":"json","target_lang":"zh"},"contract.json",json.dumps([{"item_type":"term","term":ctx["knowledge_term"],"translation":"测试知识"}]).encode())
                record={"service":service,"method":method,"path":template}
                try:
                    code,mime,obj=service_call(port,method,path,body,auth=template!="/health",form=form)
                    record.update(http_status=code,response_kind=mime,json_fields=sorted(obj) if isinstance(obj,dict) else [],
                        probe="positive" if 200<=code<300 else "rejection" if 400<=code<500 else "failed",issue=None)
                    if code==404:
                        record["issue"]="standalone_route_or_fixture_not_found"
                    elif code>=500:
                        record["issue"]="standalone_server_error"
                        if isinstance(obj,dict):record["error_detail"]=str(obj.get("detail",""))[:300]
                    if 200<=code<300 and method=="POST" and isinstance(obj,dict):
                        if template==provider_base:
                            ctx["provider_id"]=obj["id"]
                        elif template==source_base:
                            ctx["source_pk"]=obj["id"]
                        elif template==knowledge_base:
                            ctx["item_id"]=obj["id"]
                    if service=="subtitle_service" and method=="POST" and template in ("/subtitle/jobs","/subtitle/dictionaries/lookup","/subtitle/dictionaries/import","/subtitle/knowledge/import"):
                        checked=False
                        if code==200 and isinstance(obj,dict):
                            with psycopg.connect(os.environ["DATABASE_URL"]) as db:
                                if template=="/subtitle/jobs":
                                    checked=obj.get("status")=="queued" and db.execute("SELECT task_id::text,stage,status FROM operation_inbox WHERE id=%s",(obj.get("job_id"),)).fetchone()==(ctx["create_task"],"subtitle","pending")
                                elif template=="/subtitle/dictionaries/lookup":
                                    checked=any(x.get("id")==ctx["entry_id"] and x.get("translation")=="测试术语" for x in obj.get("results",[])) and db.execute("SELECT usage_count FROM translation_dictionary_entries WHERE id=%s",(ctx["entry_id"],)).fetchone()==(1,)
                                elif template=="/subtitle/dictionaries/import":
                                    checked=obj.get("upserted")==1 and obj.get("status")=="succeeded" and db.execute("SELECT e.translation_text FROM translation_dictionary_entries e JOIN translation_dictionary_sources s ON s.id=e.source_id WHERE s.slug=%s AND e.normalized_term='fixture trace'",(ctx["import_slug"],)).fetchone()==("测试走线",)
                                elif template=="/subtitle/knowledge/import":
                                    checked=obj.get("imported")==1 and obj.get("failed")==0 and db.execute("SELECT translation FROM translation_knowledge_items WHERE term=%s",(ctx["knowledge_term"],)).fetchone()==("测试知识",)
                        record["fixture_asserted"]=bool(checked)
                        if not checked:record.update(probe="failed",issue="positive_fixture_assertion_failed",error_detail=str(obj)[:300])
                    if service=="social_publisher" and template=="/login-sessions/{session_id}":
                        checked=code==200 and isinstance(obj,dict) and obj.get("id")==ctx["session_id"] and obj.get("state")==("canceled" if method=="DELETE" else "starting")
                        record["fixture_asserted"]=bool(checked)
                        if not checked:record.update(probe="failed",issue="positive_fixture_assertion_failed")
                    if template==provider_base+"/{provider_id}/remote-models":
                        if code!=200 or not isinstance(obj,dict) or obj.get("details",{}).get("model-z")!={"context_window":131072,"max_output_tokens":8192}:
                            record["probe"]="failed"
                            record["issue"]="catalog_fixture_assertion_failed"
                    if template==source_base+"/{source_pk}/scan/start" and code==202:
                        for _ in range(80):
                            sc,_,listing=service_call(port,"GET",source_base)
                            item=next((v for v in listing if v["id"]==ctx["source_pk"]),None) if sc==200 and isinstance(listing,list) else None
                            if item and not item["scan_running"] and item["last_scan_finished_at"]:
                                break
                            time.sleep(.1)
                        else:
                            record["probe"]="failed";record["issue"]="source_scan_timeout"
                    if template!="/health":
                        unauth,_,_=service_call(port,method,path,auth=False)
                        record["unauthorized_status"]=unauth
                        if unauth not in (401,403):record["issue"]="unauthenticated_request_not_rejected"
                    if code in (401,403,405) and template!="/fetch":
                        record["issue"]="standalone_auth_or_method_rejected"
                except Exception as exc:
                    record.update(http_status=None,probe="failed",issue=type(exc).__name__)
                results.append(record)
        finally:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
            service_fixtures.restore()
    assert len(results)==len(matrix["routes"])
    assert {(r["service"],r["method"],r["path"]) for r in results}=={(r["service"],r["method"],r["path"]) for r in matrix["routes"]}
summary={"standalone_egress_routes":len(standalone),
         "standalone_sau_routes":len(social),
         "standalone_service_health_routes":len(service_health),
         "probed":len(results),
         "positive":sum(r["probe"]=="positive" for r in results),
         "rejection_only":sum(r["probe"]=="rejection" for r in results),
         "failed":sum(bool(r["probe"]=="failed" or r.get("issue") in ("server_failure_or_unexpected_auth", "public_health_or_auth_status_incompatible", "unauthenticated_request_not_rejected", "standalone_auth_or_method_rejected")) for r in results),
         "blocked":sum(r["probe"]=="blocked" for r in results),
         "limited":sum(bool(r.get("issue") in ("existing_fixture_read_returned_404","standalone_route_or_fixture_not_found","native_bilibili_disabled_in_isolated_test")) for r in results),
         "full_python_parity_verified":0,
         "cookie_login_status":cookie_status,
         "environment":"disposable Postgres, mocked platform services; no production writes"}
report={"timestamp_utc":datetime.now(timezone.utc).isoformat(),
        "source_branch":"rewrite/rust-v2","summary":summary,"routes":results}
target=Path(args.out);target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
lines=[f"# VideoRoll Rust V1 部分适配 API：{len(results)} 条逐接口探测",
       "",
       f"测试日期：{report['timestamp_utc']}。",
       "环境：隔离 PostgreSQL + 本地模拟平台，不使用生产账号、生产数据库或真实投稿。",
       "已分别执行独立服务与主 API 的请求探测。" if args.all_services else f"独立 Egress Gateway 另有 {len(standalone)} 条接口，需运行专项 HTTP/SSRF 套件；不计入本 API 探测。",
       "独立服务健康检查独立计数，未将主 API 结果替代独立服务。" if args.all_services else f"独立业务服务健康检查另有 {len(service_health)} 条，由独立 Router 测试覆盖；不重复计算主 API 健康检查。",
       "通配代理的探测使用非白名单路径检查拒绝；正向参数转发由专项 Router 测试覆盖。",
       "注意：HTTP 请求得到响应不代表与 Python 完全等价；仅拒绝路径不代表正向业务通过。",
       "隔离探测不能替代 Python/Rust 双端业务差分验收。",
       "",
       f"逐项执行：{summary['probed']}；2xx 正向：{summary['positive']}；4xx 拒绝路径：{summary['rejection_only']}；异常/需要复核：{summary['failed']}；完整业务等价验收：0。",
       "",
       "| 服务 | HTTP 方法 | 原 Python 路径 | Rust 返回 | 测试类型 | 鉴权检查 | 问题 |",
       "|---|---|---|---:|---|---:|---|"]
for r in sorted(results,key=lambda x:(x["path"],x["method"])):
    lines.append(f"| {r['service']} | {r['method']} | `{r['path']}` | {r['http_status'] or '-'} | {r['probe']} | {r.get('unauthorized_status','-')} | {r.get('issue') or '-'} |")
lines += ["","## 验收边界",
          "- 本测试只能判断每个已映射入口在隔离 Rust 进程中的请求行为和未登录拒绝行为。",
          "- 正向请求仅验证返回状态/基础 JSON 结构；未完成 Python 生产响应字段、错误码、状态迁移的逐项差分。",
          "- 字幕、配音、B 站双 P、SAU 和真实 GPU 的生产级外部副作用仍需专门的受控验收。",
          "- 不对生产账号执行真实投稿、删改或会话操作。"]
# Preserve independently reviewed compatibility notes appended after the
# generated probe results; an isolated smoke test must not silently erase
# an earlier migration's documented limitations.
report_path=(target.with_suffix(".md") if args.all_services else root/"docs/CONTRACT_PARITY_64_20261122.md")
historical="\n## 2026-11-24：Remote YouTube 公网 Bearer 接口\n"
existing=report_path.read_text() if report_path.exists() else ""
appendix=existing[existing.index(historical):] if historical in existing else ""
report_path.write_text("\n".join(lines)+"\n"+appendix)
print("contract_probe_summary="+json.dumps(summary,ensure_ascii=False))
for r in results:
    if r.get("issue"):
        print("ISSUE",r["service"],r["method"],r["path"],r["http_status"],r["issue"])
# Fail the test process on actual server/auth failures, not expected business rejections.
if summary["failed"]:
    raise SystemExit(2)
