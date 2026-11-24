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
mapped = [r for r in matrix["routes"] if r["v1_status"]!="not_native_v1"
          and r["service"]!="egress_gateway"]
assert len(mapped)+len(standalone) == matrix["native_v1_partial"], "Route count and parity matrix disagree"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
anonymous = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
fake = "00000000-0000-0000-0000-000000000011"
context = {"task_id": args.task_id,"id":args.task_id,"alert_id":args.alert_id,
           "source_pk": fake, "source_type": "channel",
           "asset_id": fake,"job_id":fake,"publish_job_id":fake,
           "account_id":fake,"session_id":fake,"platform":"bilibili"}
context["provider_id"]="isolated-provider-diff-probe"
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
# Do not interfere with other probes; global worker stop/resume actions run last.
late = {"/tasks/actions/stop_all","/tasks/actions/resume_stopped",
        "/tasks/actions/resume_failed_recent","/tasks/{task_id}/actions/stop",
        "/tasks/{task_id}/actions/resume"}
# Run WebSocket before the logout route revokes the fixture's admin cookie.
# Socket auth is cookie-only; an internal HTTP token cannot substitute for it.
ordered = sorted(mapped, key=lambda r: (
    r["method"] != "WEBSOCKET", r["path"] in late, r["method"] != "GET"))
results = []
for row in ordered:
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
    for field in re.findall(r"\{([^{}]+)\}",path):
        path=path.replace("{"+field+"}",context.get(field, fake))
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
        if status>=500 or status in (301,302,303,307,308,405) or (status==401 and template not in public and not worker_wire):
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
summary={"standalone_egress_routes":len(standalone),
         "probed":len(results),
         "positive":sum(r["probe"]=="positive" for r in results),
         "rejection_only":sum(r["probe"]=="rejection" for r in results),
         "failed":sum(bool(r["probe"]=="failed" or r.get("issue") in ("server_failure_or_unexpected_auth", "public_health_or_auth_status_incompatible", "unauthenticated_request_not_rejected")) for r in results),
         "blocked":sum(r["probe"]=="blocked" for r in results),
         "limited":sum(bool(r.get("issue") in ("existing_fixture_read_returned_404","native_bilibili_disabled_in_isolated_test")) for r in results),
         "full_python_parity_verified":0,
         "cookie_login_status":cookie_status,
         "environment":"disposable Postgres, mocked platform services; no production writes"}
report={"timestamp_utc":datetime.now(timezone.utc).isoformat(),
        "source_branch":"rewrite/rust-v2","summary":summary,"routes":results}
target=Path(args.out);target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
lines=[f"# VideoRoll Rust V1 部分适配 API：{len(mapped)} 条逐接口探测",
       "",
       f"测试日期：{report['timestamp_utc']}。",
       "环境：隔离 PostgreSQL + 本地模拟平台，不使用生产账号、生产数据库或真实投稿。",
       f"独立 Egress Gateway 另有 {len(standalone)} 条接口，需运行专项 HTTP/SSRF 套件；不计入本 API 探测。",
       "注意：HTTP 请求得到响应不代表与 Python 完全等价；仅拒绝路径不代表正向业务通过。",
       "隔离探测不能替代 Python/Rust 双端业务差分验收。",
       "",
       f"逐项执行：{summary['probed']}；2xx 正向：{summary['positive']}；4xx 拒绝路径：{summary['rejection_only']}；异常/需要复核：{summary['failed']}；完整业务等价验收：0。",
       "",
       "| HTTP 方法 | 原 Python 路径 | Rust 返回 | 测试类型 | 鉴权检查 | 问题 |",
       "|---|---|---:|---|---:|---|"]
for r in sorted(results,key=lambda x:(x["path"],x["method"])):
    lines.append(f"| {r['method']} | `{r['path']}` | {r['http_status'] or '-'} | {r['probe']} | {r.get('unauthorized_status','-')} | {r.get('issue') or '-'} |")
lines += ["","## 验收边界",
          "- 本测试只能判断每个已映射入口在隔离 Rust 进程中的请求行为和未登录拒绝行为。",
          "- 正向请求仅验证返回状态/基础 JSON 结构；未完成 Python 生产响应字段、错误码、状态迁移的逐项差分。",
          "- 字幕、配音、B 站双 P、SAU 和真实 GPU 的生产级外部副作用仍需专门的受控验收。",
          "- 不对生产账号执行真实投稿、删改或会话操作。"]
# Preserve independently reviewed compatibility notes appended after the
# generated probe results; an isolated smoke test must not silently erase
# an earlier migration's documented limitations.
report_path=root/"docs/CONTRACT_PARITY_64_20261122.md"
historical="\n## 2026-11-24：Remote YouTube 公网 Bearer 接口\n"
existing=report_path.read_text() if report_path.exists() else ""
appendix=existing[existing.index(historical):] if historical in existing else ""
report_path.write_text("\n".join(lines)+"\n"+appendix)
print("contract_probe_summary="+json.dumps(summary,ensure_ascii=False))
for r in results:
    if r.get("issue"):
        print("ISSUE",r["method"],r["path"],r["http_status"],r["issue"])
# Fail the test process on actual server/auth failures, not expected business rejections.
if summary["failed"]:
    raise SystemExit(2)
