#!/usr/bin/env python3
"""Isolated true HTTP parity: YouDub status, configured Agent Skills and local RenderWorker enrolment.

Requires disposable postgres/API already started by smoke-isolated.sh, all requests
strictly loopback. Does not submit to any real platform or run a GPU model.
"""
import os,uuid,hashlib,json
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import psycopg
from psycopg.rows import dict_row

base="http://127.0.0.1:18080"
uri=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in uri and uri.endswith("/videoroll_test")
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
bootstrap=os.environ["ADMIN_BOOTSTRAP_SECRET"]
runtime=Path(os.environ["VIDEOROLL_V2_YOUDUB_RUNTIME"])
def req(method,path,body=None,headers=None):
    hd=dict(headers or {})
    data=json.dumps(body).encode() if body is not None else None
    if body is not None:hd["Content-Type"]="application/json"
    try:response=urlopen(Request(base+path,data=data,headers=hd,method=method),timeout=15)
    except HTTPError as e:response=e
    with response:
        content=response.read()
        try:content=json.loads(content)
        except (ValueError,TypeError):content=content.decode(errors="replace")
        return response.status,content
auth={"x-videoroll-v2-internal-token":token}
def q(sql,args=()):
    with psycopg.connect(uri,row_factory=dict_row) as conn:
        return conn.execute(sql,args).fetchone()
for path in ("/api/v1/settings/dubbing","/api/v1/settings/dubbing/native",
             "/api/v1/subtitle/agent/skills"):
    assert req("GET",path)[0]==401,path

status,value=req("GET","/api/v1/settings/dubbing",headers=auth)
assert status==200 and "native" in value and "worker" in value,(status,value)
status,native=req("GET","/api/v1/settings/dubbing/native",headers=auth)
assert status==200 and native==value["native"],(status,native)
for required in ["engine","installed","enabled","resident","accepting_jobs","active_jobs",
                 "busy","mode","gpu_loaded","message"]:
    assert required in native,(required,native)
assert native["engine"]=="youdub:voxcpm2"
assert native["gpu_loaded"] is not True, "mock runtime must not claim real loaded GPU"
runtime.mkdir(parents=True,exist_ok=True)
control=runtime/"videoroll-control.json"
before=control.read_bytes() if control.is_file() else None
try:
    control.write_text('{"enabled":true,"resident":true}\n',encoding="utf-8")
    status,enabled=req("GET","/api/v1/settings/dubbing/native",headers=auth)
    assert status==200 and enabled["enabled"] and enabled["resident"],(status,enabled)
    assert enabled["gpu_loaded"] is False,enabled
    assert not enabled["accepting_jobs"],"mock runtime cannot accept without real model"
finally:
    if before is None:control.unlink(missing_ok=True)
    else:control.write_bytes(before)
print("PASS: native YouDub V1 status reads actual runtime/control, never invents GPU residency, auth enforced")

status,skills=req("GET","/api/v1/subtitle/agent/skills",headers=auth)
assert status==200 and isinstance(skills,list),(status,skills)
# Same source tree / ordering / summary as the original Python SkillRegistry.
from videoroll.apps.subtitle_service.agent_skills import SkillRegistry
original=SkillRegistry.load(include_builtin=True,include_user=True).summaries()
assert skills==original,(skills,original)
assert any(x["name"]=="translation-term-research-core" and x["source"]=="builtin" for x in skills)
print("PASS: Agent Skills list equals original Python JSON/Markdown registry summaries and resource counts")

# Original bootstrap is not a user token; wrong/absent bearer must never
# rekey a worker. Both original and Rust protocols occupy the same table.
payload={
 "worker_key":"local-original-"+uuid.uuid4().hex,
 "name":"local-only worker","platform":"linux","version":"test-1",
 "render_spec_versions":[1],"protocol_version":1,
 "capabilities":{"encoders":["h264"]},"resources":{},"labels":{},
 "max_concurrency":2
}
path="/api/render-workers/v1/local-enroll"
assert req("POST",path,payload)[0]==401
assert req("POST",path,payload,{"Authorization":"Bearer wrong-secret"})[0]==401
status,first=req("POST",path,payload,{"Authorization":"Bearer "+bootstrap})
assert status==200 and first["credential"].startswith("vrw_"),(status,first)
worker=first["worker"];worker_id=worker["id"]
assert worker["worker_key"]==payload["worker_key"],worker
assert worker["labels"]["local"] and worker["labels"]["standalone"]
found=q("SELECT id,credential_hash,runtime_engine,max_concurrency FROM render_workers WHERE id=%s",
        (worker_id,))
assert found["runtime_engine"]=="python" and found["max_concurrency"]==2,found
assert found["credential_hash"]==hashlib.sha256(first["credential"].encode()).hexdigest()
status,beat=req("POST",f"/api/render-workers/v1/{worker_id}/heartbeat",{
 "status":"online","resources":{"cpu":8},"capabilities":{},"active_execution_ids":[]
},{"Authorization":"Bearer "+first["credential"]})
assert status==200 and beat["id"]==worker_id,(status,beat)
payload["max_concurrency"]=4
status,second=req("POST",path,payload,{"Authorization":"Bearer "+bootstrap})
assert status==200 and second["worker"]["id"]==worker_id,(status,second)
assert second["credential"]!=first["credential"]
assert q("SELECT max_concurrency FROM render_workers WHERE id=%s",(worker_id,))["max_concurrency"]==2
assert req("POST",f"/api/render-workers/v1/{worker_id}/heartbeat",
 {"status":"online","resources":{},"active_execution_ids":[]},
 {"Authorization":"Bearer "+first["credential"]})[0]==401
assert req("POST",f"/api/render-workers/v1/{worker_id}/heartbeat",
 {"status":"online","resources":{},"active_execution_ids":[]},
 {"Authorization":"Bearer "+second["credential"]})[0]==200
native_worker_id=uuid.uuid4();native_key="other-engine-"+uuid.uuid4().hex
with psycopg.connect(uri) as conn:
    conn.execute("""INSERT INTO render_workers(
      id,worker_key,name,platform,credential_hash,status,runtime_engine,credential_issued_at)
      VALUES(%s,%s,'native worker','linux',%s,'online','rust-v2',now())""",
      (native_worker_id,native_key,hashlib.sha256(b'native-secret').hexdigest()))
status,blocked=req("POST",path,{**payload,"worker_key":native_key},
                    {"Authorization":"Bearer "+bootstrap})
assert status==409,(status,blocked)
assert q("SELECT runtime_engine FROM render_workers WHERE id=%s",(native_worker_id,))["runtime_engine"]=="rust-v2"
print("PASS: original local RenderWorker bootstrap is private/bearer protected; original vrw_ credentials work, rotate without changing capacity; Rust worker identity cannot be overwritten")
