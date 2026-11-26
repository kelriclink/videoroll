#!/usr/bin/env python3
"""Same-fixture Python-source / Rust HTTP differential for controls and subtitles.

Never point at production. Python reference calls the original Python service
functions against the exact ephemeral Postgres the Rust HTTP API is using.
Each pair has identical initial state but unique IDs to avoid cross-mutation.
Differences in generated UUIDs/timestamps are normalized explicitly.
"""
import json,os,uuid
from types import SimpleNamespace
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from fastapi import HTTPException
from videoroll.apps.orchestrator_api.services import render_worker_service as py
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile,update_auto_profile
from videoroll.db.models import RenderWorker,RenderJob,RenderExecution,RenderWorkerEnrollment,AppSetting
from videoroll.apps.subtitle_service.schemas import SubtitleJobRead

DATABASE_URL=os.environ["DATABASE_URL"]
assert DATABASE_URL.rsplit("/",1)[-1]=="videoroll_test", "refuse non-disposable database"
engine=create_engine(DATABASE_URL.replace("postgresql://","postgresql+psycopg://",1))
BASE="http://127.0.0.1:18080/api/v1"
TOKEN=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
def request(method,path,body=None):
    headers={"x-videoroll-v2-internal-token":TOKEN,"content-type":"application/json"}
    req=Request(BASE+path,method=method,headers=headers,
                data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(req,timeout=12) as r:
            return r.status,json.loads(r.read())
    except HTTPError as e:
        return e.code,json.loads(e.read())
def execute(query,params=None):
    with engine.begin() as c:
        for statement in query.split(';'):
            if statement.strip():c.execute(text(statement),params or {})
def read(query,params=None):
    with engine.connect() as c:
        return c.execute(text(query),params or {}).mappings().first()
def same(left,right,keys):
    for key in keys:
        assert left[key]==right[key],(key,left[key],right[key])
def snapshot_worker(row):
    return py.worker_admin_payload(row)
def normalize_worker(value):
    return {k:value.get(k) for k in (
        "worker_key","name","platform","enabled","draining","max_concurrency",
        "active_jobs","available_slots","status","credential_active",
        "device_count","detected_capacity","resources","labels")}
PY_ID=uuid.UUID("00000000-0000-0000-0000-000000010101")
RU_ID=uuid.UUID("00000000-0000-0000-0000-000000010102")
TASK_PY=uuid.UUID("00000000-0000-0000-0000-000000010103")
TASK_RU=uuid.UUID("00000000-0000-0000-0000-000000010104")
JOB_PY=uuid.UUID("00000000-0000-0000-0000-000000010105")
JOB_RU=uuid.UUID("00000000-0000-0000-0000-000000010106")
EXEC_PY=uuid.UUID("00000000-0000-0000-0000-000000010107")
EXEC_RU=uuid.UUID("00000000-0000-0000-0000-000000010108")
for id,kind in ((TASK_PY,"python"),(TASK_RU,"rust")):
    execute("INSERT INTO tasks(id,source_type,source_license,status,created_by)"
            " VALUES (:id,'local','own','created',:kind)",
            {"id":id,"kind":f"compare-{kind}"})
for id,kind in ((PY_ID,"python"),(RU_ID,"rust")):
    execute("""INSERT INTO render_workers
      (id,worker_key,name,platform,architecture,version,resources,labels,
       status,enabled,draining,max_concurrency,active_jobs,credential_hash)
      VALUES(:id,:key,'Diff Render','windows','x86_64','1.2',
       '{"devices":[{"id":"GPU0","status":"busy"},{"id":"GPU1","status":"idle"}]}'::jsonb,
       '{}'::jsonb,'busy',true,false,3,1,repeat('a',64))""",
       {"id":id,"key":f"diff-{kind}"})

# Compare original Python source function with Rust HTTP and DB outcomes.
with Session(engine) as s:
    p=py.control_worker(s,PY_ID,SimpleNamespace(
       enabled=True,draining=True,max_concurrency=4,worker_key="diff-python-renamed"))
    py_worker=snapshot_worker(p)
status,ru_worker=request("PATCH",f"/render-management/workers/{RU_ID}",
   {"enabled":True,"draining":True,"max_concurrency":4,"worker_key":"diff-rust-renamed"})
assert status==200,(status,ru_worker)
a=normalize_worker(py_worker);b=normalize_worker(ru_worker)
for field in a:
    if field=="worker_key":assert a[field].endswith("-renamed") and b[field].endswith("-renamed")
    else:assert a[field]==b[field],(field,a,b)
with Session(engine) as s:
    py_w=py.control_worker_device(s,PY_ID,"GPU1",False)
    py_disabled=snapshot_worker(py_w)
status,ru_disabled=request("PATCH",f"/render-management/workers/{RU_ID}/devices/GPU1",
                           {"enabled":False})
assert status==200 and py_disabled["labels"]==ru_disabled["labels"],(status,ru_disabled)
assert py_disabled["resources"]["devices"]==ru_disabled["resources"]["devices"]
with Session(engine) as s:
    try:py.control_worker_device(s,PY_ID,"NO_DEVICE",True)
    except HTTPException as e:assert e.status_code==404
    else:raise AssertionError("Python unknown device should reject")
assert request("PATCH",f"/render-management/workers/{RU_ID}/devices/NO_DEVICE",
               {"enabled":True})[0]==404
with Session(engine) as s:
    py_revoke=snapshot_worker(py.revoke_worker_credential(s,PY_ID))
status,ru_revoke=request("POST",f"/render-management/workers/{RU_ID}/revoke-credential",{})
assert status==200,(status,ru_revoke)
for field in ("enabled","status","credential_active"):
    assert py_revoke[field]==ru_revoke[field],(field,py_revoke,ru_revoke)
print("PASS DIFF: Python/Rust worker PATCH/device control/revoke status + persisted device labels")

# Enrollment creation uses randomized tokens; compare shape, hashes, TTL and revoke.
with Session(engine) as s:
    py_invite,py_token=py.create_enrollment(s,label="Diff Invitation",ttl_minutes=15)
    py_invite_id=py_invite.id
status,invite=request("POST","/render-management/enrollments",
                      {"label":"Diff Invitation","ttl_minutes":15})
assert status==200 and invite["token"].startswith("vre_")
assert str(py_invite_id)!=invite["id"]
assert py_token.startswith("vre_")
assert len(py_token)>35 and len(invite["token"])>35
with Session(engine) as s:
    s.expire_all()
    py_invite=s.get(RenderWorkerEnrollment,py_invite_id)
    ru_invite=s.get(RenderWorkerEnrollment,uuid.UUID(invite["id"]))
    assert ru_invite.token_hash and ru_invite.status=="active"
    assert py_invite.label==ru_invite.label
    assert abs((ru_invite.expires_at-py_invite.expires_at).total_seconds())<=20
    py_revoked=py.revoke_enrollment(s,py_invite_id)
    py_status=py_revoked.status
status,ru_revoked=request("DELETE",f"/render-management/enrollments/{invite['id']}")
assert status==200 and ru_revoked["status"]==py_status=="revoked",(status,ru_revoked)
assert request("DELETE",f"/render-management/enrollments/{invite['id']}")[1]["status"]=="revoked"
print("PASS DIFF: Python/Rust invitation creation, token hashes, TTL and idempotent revoke")

# With equal lease/worker/job states, cancel and requeue independently on both.
def seed_execution(py_id,task,job_id,exe_id,key):
    execute("""INSERT INTO render_jobs(id,task_id,status,request_json,progress,
             lease_owner,lease_until,started_at)
        VALUES(:job,:task,'running','{}'::jsonb,47,:lease,now()+interval '3 minutes',now())""",
       {"job":job_id,"task":task,"lease":f"render-worker:{exe_id}"})
    execute("""INSERT INTO render_executions(id,render_job_id,worker_id,
             attempt,fence_token,state,progress,render_spec)
        VALUES(:exe,:job,:worker,1,:fence,'running',47,
             jsonb_build_object('schema_version',1,'mode','burn_in'))""",
       {"exe":exe_id,"job":job_id,"worker":py_id,"fence":key})
seed_execution(PY_ID,TASK_PY,JOB_PY,EXEC_PY,"diff-python-fence")
seed_execution(RU_ID,TASK_RU,JOB_RU,EXEC_RU,"diff-rust-fence")
with Session(engine) as s:
    p=py.cancel_execution(s,EXEC_PY,"operator canceled")
    py_cancel=py.execution_admin_payload(s,p)
status,ru_cancel=request("POST",f"/render-management/executions/{EXEC_RU}/cancel",
                        {"reason":"operator canceled"})
assert status==200,(status,ru_cancel)
for field in ("state","error_message","progress","job_status"):
    assert py_cancel[field]==ru_cancel[field],(field,py_cancel,ru_cancel)
assert py_cancel["state"]=="canceled" and py_cancel["job_status"]=="canceled"
assert request("POST",f"/render-management/executions/{EXEC_RU}/cancel",
               {"reason":"duplicate cancel"})[1]["error_message"]=="operator canceled"
def job_state(job_id):
    row=read("SELECT status,retry_count,lease_owner,progress FROM render_jobs WHERE id=:id",{"id":job_id})
    return dict(row)
same(job_state(JOB_PY),job_state(JOB_RU),["status","retry_count","lease_owner","progress"])
print("PASS DIFF: Python/Rust execution cancel, owner lease fencing and idempotent repeat")

# Fresh job: requeue must clear old lease and increment only once.
PY_JOB2=uuid.UUID("00000000-0000-0000-0000-000000010109")
RU_JOB2=uuid.UUID("00000000-0000-0000-0000-000000010110")
PY_EX2=uuid.UUID("00000000-0000-0000-0000-000000010111")
RU_EX2=uuid.UUID("00000000-0000-0000-0000-000000010112")
for worker,task,job,exe,fence in (
 (PY_ID,TASK_PY,PY_JOB2,PY_EX2,"diff-python-2"),
 (RU_ID,TASK_RU,RU_JOB2,RU_EX2,"diff-rust-2")):
    execute("UPDATE render_workers SET active_jobs=1,status='busy' WHERE id=:id",{"id":worker})
    seed_execution(worker,task,job,exe,fence)
with Session(engine) as s:
    p=py.requeue_execution(s,PY_EX2,"operator requeue")
    py_requeue=py.execution_admin_payload(s,p)
status,ru_requeue=request("POST",f"/render-management/executions/{RU_EX2}/requeue",
                          {"reason":"operator requeue"})
assert status==200,(status,ru_requeue)
for field in ("state","error_message","progress","job_status"):
    assert py_requeue[field]==ru_requeue[field],(field,py_requeue,ru_requeue)
same(job_state(PY_JOB2),job_state(RU_JOB2),["status","retry_count","lease_owner","progress"])
assert job_state(RU_JOB2)["retry_count"]==1
status,_=request("POST",f"/render-management/executions/{RU_EX2}/requeue",
                 {"reason":"duplicate requeue"})
assert status==200 and job_state(RU_JOB2)["retry_count"]==1
print("PASS DIFF: Python/Rust execution requeue; Rust repeat is safely idempotent")

# Fencing: a historical execution must never reset a *new* live lease.
new_lease="render-worker:00000000-0000-0000-0000-000000010188"
for job in (PY_JOB2,RU_JOB2):
    execute("UPDATE render_jobs SET status='running',lease_owner=:lease WHERE id=:id",
        {"id":job,"lease":new_lease})
with Session(engine) as s:
    try:py.requeue_execution(s,PY_EX2,"old execution must not reset new owner")
    except HTTPException as error: assert error.status_code==409
    else:raise AssertionError("original Python must fence old execution")
assert request("POST",f"/render-management/executions/{RU_EX2}/requeue",
    {"reason":"old execution must not reset new owner"})[0]==409
assert job_state(RU_JOB2)["lease_owner"]==new_lease
# A terminal P2 execution can be viewed/canceled idempotently even though
# active P2 requeue remains forbidden until YouDub outbox parity is available.
execute("UPDATE render_jobs SET request_json=jsonb_build_object('dubbing_job_id',CAST(:dub AS text)) WHERE id=:id",
        {"dub":"00000000-0000-0000-0000-000000010199","id":RU_JOB2})
assert request("POST",f"/render-management/executions/{RU_EX2}/cancel",
               {"reason":"already canceled"})[0]==200
assert job_state(RU_JOB2)["lease_owner"]==new_lease
print("PASS DIFF: stale execution cannot reset new lease; terminal P2 cancel remains idempotent")

# P2 is coupled to YouDub state and the durable Python outbox.
# Compare Python's actual domain function with Rust on corresponding rows.
P2_PY_DUB=uuid.UUID("00000000-0000-0000-0000-000000010201")
P2_RU_DUB=uuid.UUID("00000000-0000-0000-0000-000000010202")
P2_PY_JOB=uuid.UUID("00000000-0000-0000-0000-000000010203")
P2_RU_JOB=uuid.UUID("00000000-0000-0000-0000-000000010204")
P2_PY_EX=uuid.UUID("00000000-0000-0000-0000-000000010205")
P2_RU_EX=uuid.UUID("00000000-0000-0000-0000-000000010206")
for worker,task,dub,job,exe in (
    (PY_ID,TASK_PY,P2_PY_DUB,P2_PY_JOB,P2_PY_EX),
    (RU_ID,TASK_RU,P2_RU_DUB,P2_RU_JOB,P2_RU_EX)):
    execute("UPDATE render_workers SET active_jobs=1,status='busy' WHERE id=:id",{"id":worker})
    execute("""INSERT INTO dubbing_jobs(id,task_id,status,progress,output_json)
               VALUES (:id,:task,'rendering',75,
               jsonb_build_object('youdub',jsonb_build_object('stage','rendering_p2')))""",
            {"id":dub,"task":task})
    seed_execution(worker,task,job,exe,"p2-fence-"+str(dub)[-4:])
    execute("""UPDATE render_jobs
               SET request_json=jsonb_build_object('dubbing_job_id',CAST(:dub AS text))
               WHERE id=:id""",{"dub":str(dub),"id":job})
    execute("UPDATE dubbing_jobs SET output_json=output_json||jsonb_build_object('render_job_id',CAST(:job AS text)) WHERE id=:dub",
            {"job":str(job),"dub":dub})
with Session(engine) as s:
    p=py.cancel_execution(s,P2_PY_EX,"operator canceled P2")
    py_cancel=py.execution_admin_payload(s,p)
# A Python-owned task cannot be canceled by the Rust P2 path. The
# complete transaction must roll back, including lease and worker slot.
def p2_snapshot(dub,job,exe):
    return (dict(read("SELECT status,error_message,output_json FROM dubbing_jobs WHERE id=:id",{"id":dub})),
            job_state(job),dict(read("SELECT state,error_message FROM render_executions WHERE id=:id",{"id":exe})),
            read("SELECT active_jobs FROM render_workers WHERE id=:id",{"id":RU_ID})["active_jobs"])
before=p2_snapshot(P2_RU_DUB,P2_RU_JOB,P2_RU_EX)
path=f"/render-management/executions/{P2_RU_EX}/cancel"
assert request("POST",path,{"reason":"operator canceled P2"})[0]==409
assert p2_snapshot(P2_RU_DUB,P2_RU_JOB,P2_RU_EX)==before
# Model an already audited owner cutover using only the disposable fixture.
execute("UPDATE tasks SET runtime_owner='rust' WHERE id=:id",{"id":TASK_RU})
execute("UPDATE dubbing_jobs SET output_json=output_json||jsonb_build_object('render_job_id','wrong') WHERE id=:id",{"id":P2_RU_DUB})
wrong=p2_snapshot(P2_RU_DUB,P2_RU_JOB,P2_RU_EX)
assert request("POST",path,{"reason":"operator canceled P2"})[0]==409
assert p2_snapshot(P2_RU_DUB,P2_RU_JOB,P2_RU_EX)==wrong
execute("UPDATE dubbing_jobs SET output_json=output_json||jsonb_build_object('render_job_id',CAST(:job AS text)) WHERE id=:id",{"job":str(P2_RU_JOB),"id":P2_RU_DUB})
code,ru_cancel=request("POST",path,{"reason":"operator canceled P2"})
assert code==200,(code,ru_cancel)
for field in ("state","error_message","job_status"):
    assert py_cancel[field]==ru_cancel[field],(field,py_cancel,ru_cancel)
py_dub=read("SELECT status,error_message,output_json FROM dubbing_jobs WHERE id=:id",{"id":P2_PY_DUB})
ru_dub=read("SELECT status,error_message,output_json FROM dubbing_jobs WHERE id=:id",{"id":P2_RU_DUB})
for field in ("status","error_message"):
    assert py_dub[field]==ru_dub[field],(field,py_dub,ru_dub)
assert ru_dub["output_json"]=={**py_dub["output_json"],"render_job_id":str(P2_RU_JOB)}
assert ru_dub["status"]=="failed" and ru_dub["error_message"]=="P2 render canceled: operator canceled P2"
py_evt=read("SELECT * FROM outbox_events WHERE aggregate_id=:id",{"id":str(P2_PY_DUB)})
assert py_evt and py_evt["task_name"]=="subtitle_service.push_dubbing_workflow_event"
key=f"workflow-dubbing-finished:{P2_RU_DUB}:failed"
def receipt():
    return read("SELECT runtime_engine,status,task_id,request_json,result_json FROM operation_inbox WHERE operation_key=:key",{"key":key})
row=receipt()
assert row and row["runtime_engine"]=="rust-render-events" and row["status"]=="completed"
assert row["task_id"]==TASK_RU
assert row["request_json"]=={"dubbing_job_id":str(P2_RU_DUB),"render_job_id":str(P2_RU_JOB),"execution_id":str(P2_RU_EX)}
assert row["result_json"]=={"status":"failed","error":ru_dub["error_message"]}
assert request("POST",path,{"reason":"duplicate cancel P2"})[0]==200
assert receipt()==row
assert read("SELECT count(*) AS n FROM outbox_events WHERE aggregate_id=:id",{"id":str(P2_RU_DUB)})["n"]==0
print("PASS DIFF: P2 cancellation business outcome matches Python; native receipt replaces old broker event; wrong binding and Python owner roll back")

P2_PY_DUB2=uuid.UUID("00000000-0000-0000-0000-000000010207")
P2_RU_DUB2=uuid.UUID("00000000-0000-0000-0000-000000010208")
P2_PY_JOB2=uuid.UUID("00000000-0000-0000-0000-000000010209")
P2_RU_JOB2=uuid.UUID("00000000-0000-0000-0000-000000010210")
P2_PY_EX2=uuid.UUID("00000000-0000-0000-0000-000000010211")
P2_RU_EX2=uuid.UUID("00000000-0000-0000-0000-000000010212")
for worker,task,dub,job,exe in (
    (PY_ID,TASK_PY,P2_PY_DUB2,P2_PY_JOB2,P2_PY_EX2),
    (RU_ID,TASK_RU,P2_RU_DUB2,P2_RU_JOB2,P2_RU_EX2)):
    execute("UPDATE render_workers SET active_jobs=1,status='busy' WHERE id=:id",{"id":worker})
    execute("""INSERT INTO dubbing_jobs(id,task_id,status,progress,output_json,error_message)
               VALUES (:id,:task,'paused',47,
               jsonb_build_object('youdub',jsonb_build_object('stage','paused')),
               'pause awaiting approval')""",{"id":dub,"task":task})
    seed_execution(worker,task,job,exe,"p2-requeue-"+str(dub)[-4:])
    execute("""UPDATE render_jobs
               SET request_json=jsonb_build_object('dubbing_job_id',CAST(:dub AS text))
               WHERE id=:id""",{"dub":str(dub),"id":job})
    execute("UPDATE dubbing_jobs SET output_json=output_json||jsonb_build_object('render_job_id',CAST(:job AS text)) WHERE id=:dub",
            {"job":str(job),"dub":dub})
with Session(engine) as s:
    p=py.requeue_execution(s,P2_PY_EX2,"operator resume P2")
    py_result=py.execution_admin_payload(s,p)
# Python can requeue a historical job into its broker; Rust-owned legacy
# rows intentionally cannot be requeued into a retired consumer.
before=p2_snapshot(P2_RU_DUB2,P2_RU_JOB2,P2_RU_EX2)
code,ru_result=request("POST",f"/render-management/executions/{P2_RU_EX2}/requeue",
                       {"reason":"operator resume P2"})
assert code==409,(code,ru_result)
assert p2_snapshot(P2_RU_DUB2,P2_RU_JOB2,P2_RU_EX2)==before
py_dub=read("SELECT status,error_message,output_json FROM dubbing_jobs WHERE id=:id",{"id":P2_PY_DUB2})
assert py_dub["status"]=="rendering" and job_state(P2_PY_JOB2)["retry_count"]==1
assert read("SELECT count(*) AS n FROM outbox_events WHERE aggregate_id=:id",{"id":str(P2_RU_DUB2)})["n"]==0
print("PASS intentional difference: Python legacy P2 requeue works; Rust-owned legacy P2 requeue returns 409 without mutation")

# Subtitle profile: compare same default Python normalized dictionary and Rust GET.
execute("DELETE FROM app_settings WHERE key='subtitle.auto_profile'")
with Session(engine) as s: py_defaults=get_auto_profile(s)
status,ru_defaults=request("GET","/subtitle/auto/profile")
assert status==200 and py_defaults==ru_defaults,(py_defaults,ru_defaults)
update={"translate_enabled":False,"bilingual":True,"target_lang":"ja",
        "asr_language":"en","translate_style":"准确"}
with Session(engine) as s: py_updated=update_auto_profile(s,update)
# Restore the same pre-update legacy data to compare Rust write in isolation.
execute("DELETE FROM app_settings WHERE key='subtitle.auto_profile'")
status,ru_updated=request("PUT","/subtitle/auto/profile",update)
assert status==200,(status,ru_updated)
assert py_updated==ru_updated,(py_updated,ru_updated)
assert read("SELECT count(*) AS n FROM app_settings WHERE key='subtitle.auto_profile'")["n"]==1
assert request("PUT","/subtitle/auto/profile",{"auto_publish":True})[0]==409
assert request("GET","/subtitle/auto/profile")[1]==ru_updated
print("PASS DIFF: Python/Rust subtitle profile defaults and supported updates; Rust updates original app_settings row")

# Original Python SubtitleJobRead model and artifact projection vs Rust handler.
SUB_JOB=uuid.UUID("00000000-0000-0000-0000-000000010113")
ASSET=uuid.UUID("00000000-0000-0000-0000-000000010114")
execute("""INSERT INTO subtitle_jobs(id,task_id,status,progress,logs_key,error_message)
 VALUES (:j,:t,'running',63,'v2/sub/test.log',NULL);
 INSERT INTO assets(id,task_id,kind,storage_key)
 VALUES (:a,:t,'subtitle_srt'::asset_kind,'v2/sub/test.srt')""",
 {"j":SUB_JOB,"t":TASK_RU,"a":ASSET})
with Session(engine) as s:
    from videoroll.db.models import SubtitleJob,Asset,AssetKind
    j=s.get(SubtitleJob,SUB_JOB)
    artifacts=s.query(Asset).filter(Asset.task_id==j.task_id,
         Asset.kind.in_([AssetKind.audio_wav,AssetKind.segments_json,
            AssetKind.subtitle_srt,AssetKind.subtitle_ass,AssetKind.video_final,
            AssetKind.log])).order_by(Asset.created_at.asc()).all()
    py_job=SubtitleJobRead(job_id=j.id,task_id=j.task_id,status=j.status.value,
        progress=j.progress,artifacts=[{"kind":a.kind.value,"key":a.storage_key} for a in artifacts],
        logs_key=j.logs_key,error_message=j.error_message,
        created_at=j.created_at,updated_at=j.updated_at).model_dump(mode="json")
status,ru_job=request("GET",f"/subtitle/jobs/{SUB_JOB}")
assert status==200,(status,ru_job)
assert py_job==ru_job,(py_job,ru_job)
assert request("GET","/subtitle/jobs/00000000-0000-0000-0000-000000010199")[0]==404
print("PASS DIFF: Python SubtitleJobRead schema/artifact projection identical to Rust on same DB row")

# Native subtitle quality/context GETs must reflect the exact Python DTO,
# including nested sanitized memory and filesystem artifacts.
from pathlib import Path as _Path
from videoroll.apps.subtitle_service.main import (
    get_task_subtitle_quality as py_quality_handler,
    get_task_translation_context as py_context_handler,
)
assert request("GET",f"/subtitle/tasks/{TASK_RU}/quality")[1]=={
    "available":False,"job_id":str(SUB_JOB),"report":None}
with Session(engine) as s:
    settings=SimpleNamespace(storage_root=os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
    assert request("GET",f"/subtitle/tasks/{TASK_RU}/translation-context")[1]==(
        py_context_handler(TASK_RU,db=s,settings=settings))
root=_Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"]).resolve()
quality_key=f"sub/{TASK_RU}/quality/source-summary.json"
context_key=f"sub/{TASK_RU}/translation_context/context.json"
quality={"version":1,"overall_score":0.94,
         "alerts":[{"kind":"repetition","indices":[2]}],"segments":3}
context={"version":1,"summary":"Scene opens with narration",
         "memory":{"topic":"  Deep  space  ",
                   "style_notes":"clear captions",
                   "characters":[{"name":"Alice","target_name":"爱丽丝",
                                  "aliases":[" A "," A ","Ally"],"role":" lead "},
                                 {"name":"","target_name":""}],
                   "terminology":[{"source":"warp drive","target":"曲速引擎","meaning":"engine"},
                                  {"source":"invalid","target":""}],
                   "ambiguities":[{"term":"drive","resolution":"发动机"}],
                   "recent_scene":{"scene_id":2,"summary":"arrival at station"}}}
for name,value in ((quality_key,quality),(context_key,context)):
    path=root.joinpath(name)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
artifacts_json=json.dumps({"artifacts":{
  "subtitle_quality_key":quality_key,"translation_context_key":context_key}})
execute("UPDATE subtitle_jobs SET request_json=CAST(:data AS jsonb) WHERE id=:id",
        {"id":SUB_JOB,"data":artifacts_json})
with Session(engine) as s:
    settings=SimpleNamespace(storage_root=str(root))
    py_quality=py_quality_handler(TASK_RU,db=s,settings=settings)
    py_context=py_context_handler(TASK_RU,db=s,settings=settings)
assert request("GET",f"/subtitle/tasks/{TASK_RU}/quality")== (200,py_quality)
assert request("GET",f"/subtitle/tasks/{TASK_RU}/translation-context")== (200,py_context)
assert py_context["memory"]["characters"][0]["aliases"]==["A","Ally"]
assert request("GET","/subtitle/tasks/00000000-0000-0000-0000-000000010199/quality")[0]==404
# Stored path cannot escape root or follow symlinks.
execute("""UPDATE subtitle_jobs SET request_json=jsonb_build_object(
  'artifacts',jsonb_build_object('subtitle_quality_key','../private.json'))
  WHERE id=:id""",{"id":SUB_JOB})
assert request("GET",f"/subtitle/tasks/{TASK_RU}/quality")[0]==400
print("PASS DIFF: original Python/Rust subtitle quality + translation context identical on same job and JSON files, path traversal denied")


# Provider catalog: original Python list_providers + get_translate_settings
# run against the same isolated app_settings rows as Rust.
from videoroll.config import SubtitleServiceSettings
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.apps.subtitle_service.model_provider_store import list_providers
tsettings={
 "openai":{
   "base_url":"https://translate.example.net/v1",
   "model":"qwen-test-27b",
   "temperature":0.6,
   "timeout_seconds":91,
   "max_retries":4,
   "api_type":"cerebras",
   "enable_thinking":True,
   "cerebras_reasoning_effort":"high",
   "cerebras_reasoning_format":"parsed"
 },
 "index_temperature":0.0,
 "index_timeout_seconds":120.0,
 "index_max_tokens":4096,
 "index_batch_size":8,
 "index_context_chars":4096,
 "index_domain":"轨道交通",
 "index_glossary":"axle=车轴",
 "index_instruction":"保留技术术语",
 "default_max_retries":4
}
catalog_rows={"providers":[
 {"id":"diff-custom-openai","name":"自建翻译服务",
  "base_url":"https://custom.example.test/v1","api_type":"openai",
  "api_key_enc":"fake-secret-encrypted-token-never-output",
  "models":[{"id":"model-large","context_window":131072,
             "max_output_tokens":8192,"temperature":0.75,
             "enable_thinking":False,"reasoning_effort":"medium",
             "reasoning_format":"parsed"}],
  "timeout_seconds":120,"max_retries":2}]}
for key,value in (("subtitle.translate",tsettings),
                  ("subtitle.model_providers",catalog_rows)):
    execute("""INSERT INTO app_settings(key,value_json) VALUES(:key,CAST(:data AS jsonb))
          ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json""",
          {"key":key,"data":json.dumps(value,ensure_ascii=False)})
with Session(engine) as s:
    defaults=SubtitleServiceSettings(_env_file=None,REDIS_URL="redis://127.0.0.1:1/99")
    original=list_providers(s,get_translate_settings(s,defaults))
code,new=request("GET","/subtitle/translate/model-providers")
assert code==200,(code,new)
assert new==original,(new,original)
assert all("api_key_enc" not in repr(item) for item in new)
assert "fake-secret-encrypted-token-never-output" not in str(new)
assert new[0]["kind"]=="compatibility"
assert new[1]["kind"]=="builtin"
assert new[2]["models"][0]["context_window"]==131072
print("PASS DIFF: source-original Python/Rust builtin + custom provider catalog, model limits, and secret redaction identical")


# Source-backed custom model provider CRUD: independent Python and Rust IDs,
# same disposable app_settings catalog, exact response semantics otherwise.
from videoroll.apps.subtitle_service.model_provider_store import (
    save_provider as python_save_provider,
    delete_provider as python_delete_provider,
)
provider_body={
    "name":"New Provider Test","base_url":"https://service.example.org",
    "api_key":"very-private-test-token","api_type":"cerebras",
    "timeout_seconds":101,"max_retries":2,
    "models":[{"id":"model-42","context_window":262144,
               "max_output_tokens":16384,"temperature":0.75,
               "enable_thinking":True,"reasoning_effort":"high",
               "reasoning_format":"hidden"}]
}
def normalize_public(row):
    return {k:v for k,v in row.items() if k!="id"}
with Session(engine) as s:
    py_created=python_save_provider(s,None,provider_body)
code,ru_created=request("POST","/subtitle/translate/model-providers",provider_body)
assert code==200,(code,ru_created)
assert normalize_public(py_created)==normalize_public(ru_created),(py_created,ru_created)
assert ru_created["api_key_set"] is True
assert "very-private-test-token" not in str(ru_created)
assert ru_created["models"][0]["context_window"]==262144
rust_id=ru_created["id"];py_id=py_created["id"]
assert rust_id!=py_id
row=read("SELECT value_json FROM app_settings WHERE key='subtitle.model_providers'")
rust_entry=next(v for v in row["value_json"]["providers"] if v["id"]==rust_id)
python_entry=next(v for v in row["value_json"]["providers"] if v["id"]==py_id)
assert rust_entry["api_key_enc"]==python_entry["api_key_enc"]=="dbplain:v1:very-private-test-token"
assert "api_key_enc" not in ru_created
print("PASS DIFF: Python/Rust model provider creation has identical persisted key format, limits, and safe response")

update_body={
    "name":"Updated Provider","base_url":"127.0.0.1:18082",
    "api_key":None,"api_type":"openai",
    "timeout_seconds":125,"max_retries":4,
    "models":[{"id":"renamed-model","context_window":131072,
               "max_output_tokens":4096,"temperature":None,
               "enable_thinking":False,"reasoning_effort":"low",
               "reasoning_format":"parsed"}]
}
with Session(engine) as s:
    py_updated=python_save_provider(s,py_id,update_body)
code,ru_updated=request("PUT",f"/subtitle/translate/model-providers/{rust_id}",update_body)
assert code==200,(code,ru_updated)
assert normalize_public(py_updated)==normalize_public(ru_updated),(py_updated,ru_updated)
row=read("SELECT value_json FROM app_settings WHERE key='subtitle.model_providers'")
rust_entry=next(v for v in row["value_json"]["providers"] if v["id"]==rust_id)
assert rust_entry["api_key_enc"]=="dbplain:v1:very-private-test-token"
assert ru_updated["models"][0]["temperature"] is None
print("PASS DIFF: Python/Rust provider edit retains old credential when API key is null; token budget and model rename agree")

invalid_body={**update_body,"models":[{"id":"bad","context_window":100,
                                      "max_output_tokens":100}]}
with Session(engine) as s:
    try:python_save_provider(s,py_id,invalid_body)
    except ValueError:pass
    else:raise AssertionError("Python invalid token budget unexpectedly accepted")
code,_=request("PUT",f"/subtitle/translate/model-providers/{rust_id}",invalid_body)
assert code==422,code
assert read("SELECT value_json FROM app_settings WHERE key='subtitle.model_providers'")["value_json"]["providers"][-1]==rust_entry
code,_=request("PUT",f"/subtitle/translate/model-providers/{rust_id}",
     {**update_body,"models":[{"id":"duplicate"},{"id":"duplicate"}]})
assert code==422
code,_=request("PUT","/subtitle/translate/model-providers/legacy",update_body)
assert code==422
print("PASS DIFF: invalid model budget/duplicate model/builtin write rejected without modifying catalog")

clear_body={**update_body,"api_key":""}
with Session(engine) as s:
    py_cleared=python_save_provider(s,py_id,clear_body)
code,ru_cleared=request("PUT",f"/subtitle/translate/model-providers/{rust_id}",clear_body)
assert code==200 and normalize_public(py_cleared)==normalize_public(ru_cleared)
assert ru_cleared["api_key_set"] is False
with Session(engine) as s:
    python_delete_provider(s,py_id)
code,deleted=request("DELETE",f"/subtitle/translate/model-providers/{rust_id}")
assert code==200 and deleted=={"status":"deleted"},(code,deleted)
assert request("DELETE",f"/subtitle/translate/model-providers/{rust_id}")[0]==422
assert request("DELETE","/subtitle/translate/model-providers/index_translate")[0]==422
remaining=read("SELECT value_json FROM app_settings WHERE key='subtitle.model_providers'")
assert all(item["id"] not in (py_id,rust_id) for item in remaining["value_json"]["providers"])
assert request("GET","/subtitle/translate/model-providers")[0]==200
print("PASS DIFF: provider credential clearing and delete, no duplicate records or broken builtin rows")


# Python's built-in provider controller updates subtitle.translate, not the catalog.
from videoroll.apps.subtitle_service.main import ModelProviderPayload, _updated_builtin_model_provider
def get_saved_translate():
    return read("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")["value_json"]
def restore_saved_translate(value):
    execute("UPDATE app_settings SET value_json=CAST(:payload AS jsonb) WHERE key='subtitle.translate'",
            {"payload":json.dumps(value,ensure_ascii=False)})
legacy_payload={
 "name":"Builtin compatibility","base_url":"https://new-compat.example.test",
 "api_type":"cerebras","timeout_seconds":240,"max_retries":2,
 "models":[{"id":"new-model","temperature":0.45,"enable_thinking":True,
            "reasoning_effort":"high","reasoning_format":"hidden"}]
}
before=get_saved_translate()
with Session(engine) as session:
    expected=_updated_builtin_model_provider("legacy",ModelProviderPayload(**legacy_payload),
                 settings=defaults,db=session)
    expected_settings=get_saved_translate()
restore_saved_translate(before)
status,actual=request("PUT","/subtitle/translate/model-providers/legacy",legacy_payload)
assert status==200,(status,actual)
assert expected==actual,(expected,actual)
after=get_saved_translate()
for field in ("base_url","model","temperature","timeout_seconds","max_retries",
              "api_type","enable_thinking","cerebras_reasoning_effort","cerebras_reasoning_format"):
    assert expected_settings["openai"][field]==after["openai"][field],field
assert request("PUT","/subtitle/translate/model-providers/legacy",
               {**legacy_payload,"models":[{"id":"bad","context_window":5000}]})[0]==422
print("PASS DIFF: built-in OpenAI compatibility provider updates the real translation settings")


index_payload={
 "name":"Index-Translate（官方免费）","base_url":"https://index-translate.bilibili.com/v1",
 "api_type":"index_translate","timeout_seconds":165,"max_retries":2,
 "models":[{"id":"Index-Translate-35B-A3B","temperature":0.35,
            "max_output_tokens":6000,"enable_thinking":False,
            "reasoning_effort":"medium","reasoning_format":"hidden"}],
 "options":{"batch_size":17,"context_chars":10000,"domain":"机械课程设计",
            "glossary":"axle=轴","instruction":"保留技术术语"}
}
before=get_saved_translate()
with Session(engine) as session:
    expected=_updated_builtin_model_provider("index_translate",ModelProviderPayload(**index_payload),
                  settings=defaults,db=session)
    expected_settings=get_saved_translate()
restore_saved_translate(before)
status,actual=request("PUT","/subtitle/translate/model-providers/index_translate",index_payload)
assert status==200,(status,actual)
assert expected==actual,(expected,actual)
after=get_saved_translate()
for field in ("index_temperature","index_timeout_seconds","index_max_tokens",
              "index_batch_size","index_context_chars","index_domain",
              "index_glossary","index_instruction"):
    assert expected_settings[field]==after[field],(field,expected_settings[field],after[field])
assert request("PUT","/subtitle/translate/model-providers/index_translate",
               {**index_payload,"timeout_seconds":5})[0]==422
assert get_saved_translate()==after
status,shared=request("PUT","/subtitle/translate/model-providers/legacy",legacy_payload)
assert status==200,(status,shared)
assert read("SELECT value_json FROM app_settings WHERE key='subtitle.translate'") is not None
print("PASS DIFF: built-in Index-Translate and legacy settings use one original config row")


# Original Python model catalog parser vs native Rust, same provider storage.
# The only outbound service is the loopback mock started by smoke-isolated.sh.
from videoroll.apps.subtitle_service.model_provider_store import fetch_remote_model_catalog
old_catalog=read("SELECT value_json FROM app_settings WHERE key='subtitle.model_providers'")["value_json"]
remote_rows=list(old_catalog["providers"])+[
    {"id":"remote-catalog-test","name":"Test model catalog",
     "base_url":"http://127.0.0.1:18088/v1",
     "api_key_enc":"dbplain:v1:model-test-key","api_type":"openai",
     "models":[],"timeout_seconds":15,"max_retries":1},
    {"id":"restricted-host-test","name":"Untrusted metadata address",
     "base_url":"http://169.254.169.254",
     "api_key_enc":"","api_type":"openai",
     "models":[],"timeout_seconds":15,"max_retries":1}
]
execute("UPDATE app_settings SET value_json=CAST(:payload AS jsonb) WHERE key='subtitle.model_providers'",
        {"payload":json.dumps({"providers":remote_rows})})
with Session(engine) as session:
    original=fetch_remote_model_catalog(session,"remote-catalog-test")
status,rust_catalog=request("GET","/subtitle/translate/model-providers/remote-catalog-test/remote-models")
assert status==200,(status,rust_catalog)
assert rust_catalog==original,(rust_catalog,original)
assert rust_catalog["models"]==sorted(rust_catalog["models"])
assert rust_catalog["details"]["model-z"]=={"context_window":131072,"max_output_tokens":8192}
assert rust_catalog["details"]["model-a"]=={"context_window":1000,"max_output_tokens":None}
assert request("GET","/subtitle/translate/model-providers/missing-provider/remote-models")[0]==400
# Do not send a network request to any metadata or private IP endpoint.
assert request("GET","/subtitle/translate/model-providers/restricted-host-test/remote-models")[0]==400
print("PASS DIFF: Python/Rust remote model directory and published limits match; metadata hosts are blocked")


# V1 ASR settings GET/PUT: compare actual Python store and HTTP projection
# against Rust in the same disposable PostgreSQL without real ASR requests.
from videoroll.apps.subtitle_service.asr_settings_store import (
    get_asr_settings as python_get_asr_settings,
    update_asr_settings as python_update_asr_settings
)
from videoroll.apps.subtitle_service.schemas import ASRDefaultsRead
with Session(engine) as s:
    py_asr_default=ASRDefaultsRead(**python_get_asr_settings(s,defaults)).model_dump()
code,ru_asr_default=request("GET","/subtitle/asr/settings")
assert code==200,(code,ru_asr_default)
assert ru_asr_default==py_asr_default,(ru_asr_default,py_asr_default)
asr_payload={
 "default_engine":"groq-whisper","default_language":"zh",
 "default_model":"whisper-large-v3","openvino_device":"CPU",
 "openvino_num_beams":3,"openvino_max_new_tokens":768,
 "openvino_vad_enabled":False,"openvino_vad_threshold":0.65,
 "model_download_proxy":"http://proxy.example.test:7890",
 "external_whisper_base_url":"https://whisper.example.test",
 "external_whisper_model":"whisper-batch",
 "external_whisper_batch_size":3,
 "external_whisper_max_concurrency":2,
 "external_whisper_vad_enabled":False,
 "external_whisper_vad_threshold":0.8,
 "external_whisper_min_silence_ms":850,
 "external_whisper_speech_pad_ms":120,
 "external_whisper_condition_on_previous_text":False,
 "external_whisper_max_segment_seconds":9.5,
 "external_whisper_max_segment_chars":75,
 "groq_whisper_model":"whisper-large-v3",
 "cloudflare_workers_ai_account_id":"test-account",
 "cloudflare_workers_ai_model":"@cf/openai/whisper-large-v3-turbo",
 "external_whisper_api_key":"test-external-token",
 "groq_whisper_api_key":"test-groq-token",
 "cloudflare_workers_ai_api_key":"test-cloudflare-token"
}
with Session(engine) as s:
    before=s.execute(text("SELECT value_json,version FROM app_settings WHERE key='subtitle.asr'")).mappings().first()
    py_asr=ASRDefaultsRead(**python_update_asr_settings(s,defaults,asr_payload)).model_dump()
    python_asr_record=s.execute(text("SELECT value_json,version FROM app_settings WHERE key='subtitle.asr'")).mappings().one()
    python_asr_store=dict(python_asr_record["value_json"])
    if before is None:
        s.execute(text("DELETE FROM app_settings WHERE key='subtitle.asr'"))
    else:
        s.execute(text("""UPDATE app_settings SET value_json=CAST(:record AS jsonb),
                         version=:version WHERE key='subtitle.asr'"""),
                 {"record":json.dumps(before["value_json"]), "version":before["version"]})
    s.commit()
code,ru_asr=request("PUT","/subtitle/asr/settings",asr_payload)
assert code==200,(code,ru_asr)
assert ru_asr==py_asr,(ru_asr,py_asr)
ru_record=read("SELECT value_json,version FROM app_settings WHERE key='subtitle.asr'")
assert ru_record["value_json"]==python_asr_store,(ru_record,python_asr_store)
assert ru_record["version"]==2
for platform in ("external_whisper","groq_whisper","cloudflare_workers_ai"):
    assert ru_record["value_json"][platform]["api_key_enc"].startswith("dbplain:v1:")
for field in ("external_whisper_api_key","groq_whisper_api_key","cloudflare_workers_ai_api_key"):
    assert field not in ru_asr
print("PASS DIFF: Python/Rust ASR settings complete stored schema, 3 credential formats, and GET/PUT response")
invalid_before=read("SELECT value_json FROM app_settings WHERE key='subtitle.asr'")["value_json"]
code,_=request("PUT","/subtitle/asr/settings",{"default_engine":"invalid-engine"})
assert code in (400,422)
assert read("SELECT value_json FROM app_settings WHERE key='subtitle.asr'")["value_json"]==invalid_before
code,_=request("PUT","/subtitle/asr/settings",{"external_whisper_batch_size":99})
assert code in (400,422)
assert read("SELECT value_json FROM app_settings WHERE key='subtitle.asr'")["value_json"]==invalid_before
print("PASS DIFF: invalid ASR settings are rejected atomically, no partial write")


# Translation advanced settings: source-backed 57-field Python DTO, preserved
# original app_settings entity and post-write normalization.
from videoroll.apps.subtitle_service.translate_settings_store import (
    get_translate_settings as py_get_translation,
    update_translate_settings as py_update_translation)
from videoroll.apps.subtitle_service.schemas import TranslateSettingsRead
with Session(engine) as s:
    expected=TranslateSettingsRead(**py_get_translation(s,defaults)).model_dump()
code,actual=request("GET","/subtitle/translate/settings")
assert code==200,(code,actual)
assert expected==actual,(expected,actual)
assert len(actual)==57
print("PASS DIFF: Python/Rust 57-field translation settings GET identical")
translate_payload={
 "default_provider":"index_translate","default_target_lang":"fr",
 "default_style":"technical","default_batch_size":35,
 "default_max_retries":3,"default_enable_summary":False,
 "index_temperature":0.35,"index_timeout_seconds":145,
 "index_max_tokens":7200,"index_batch_size":9,"index_context_chars":12000,
 "index_domain":"电子机械","index_glossary":"axle=轴",
 "index_instruction":"保留工程术语",
 "openai_base_url":"https://translator.example.net",
 "openai_model":"model-99","openai_temperature":0.55,
 "openai_timeout_seconds":135,"openai_max_retries":6,
 "openai_api_type":"cerebras","openai_enable_thinking":True,
 "cerebras_reasoning_effort":"high","cerebras_reasoning_format":"raw",
 "rag_enabled":True,"rag_top_k":12,"rag_min_score":0.75,
 "rag_embedding_provider":"local","rag_embedding_model":"bge-m3",
 "rag_embedding_dimensions":1024,
 "rag_embedding_model_dir":"/models/new-embed","rag_embedding_device":"cpu",
 "rag_embedding_base_url":"https://embedding.example.net",
 "rag_embedding_timeout_seconds":75,
 "rag_auto_discover_terms":True,"rag_auto_learn_terms":False,
 "rag_dictionary_enabled":True,"rag_dictionary_top_k":12,
 "rag_dictionary_min_quality":0.34,
 "rag_dictionary_auto_promote":True,"rag_wiki_enabled":True,
 "rag_search_enabled":True,"rag_search_url":"http://search.example.test",
 "rag_search_categories":"general, science, general",
 "rag_search_engines":" bing,  baidu, bing ",
 "rag_search_fallback_engines":" baidu , google ",
 "rag_search_language":"zh","rag_search_safesearch":1,
 "rag_search_time_range":"day","rag_search_pageno":4,
 "rag_domain":"材料工程","rag_agent_parallelism":3,
 "rag_agent_timeout_seconds":180,
 "rag_agent_skills_enabled":True,
 "rag_agent_builtin_skills_enabled":False,
 "rag_agent_user_skills_enabled":True,
 "openai_api_key":"translation-test-token",
 "rag_embedding_api_key":"embedding-test-token"
}
saved_before=read("SELECT value_json,version FROM app_settings WHERE key='subtitle.translate'")
with Session(engine) as session:
    py_after=TranslateSettingsRead(**py_update_translation(
        session,defaults,translate_payload)).model_dump()
py_store=read("SELECT value_json,version FROM app_settings WHERE key='subtitle.translate'")
execute("""UPDATE app_settings SET value_json=CAST(:payload AS jsonb),version=:version
           WHERE key='subtitle.translate'""",{
    "payload":json.dumps(saved_before["value_json"],ensure_ascii=False),
    "version":saved_before["version"]})
status,ru_after=request("PUT","/subtitle/translate/settings",translate_payload)
assert status==200,(status,ru_after)
assert py_after==ru_after,(py_after,ru_after)
ru_store=read("SELECT value_json,version FROM app_settings WHERE key='subtitle.translate'")
assert py_store["value_json"]==ru_store["value_json"],(py_store,ru_store)
assert ru_after["openai_api_key_set"] is True
assert ru_after["rag_embedding_api_key_set"] is True
assert "translation-test-token" not in str(ru_after)
assert "embedding-test-token" not in str(ru_after)
print("PASS DIFF: Python/Rust translation advanced settings PUT, RAG, index, model and secrets identical")
before_bad=ru_store["value_json"]
assert request("PUT","/subtitle/translate/settings",{"index_max_tokens":20000})[0] in (400,422)
assert request("PUT","/subtitle/translate/settings",{"default_provider":"not-exists"})[0] in (400,422)
assert read("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")["value_json"]==before_bad
print("PASS DIFF: invalid translation settings rejected atomically")


# Subtitle queue: compare Python's real lease-aware projection of legacy rows
# with the native Rust queue endpoint, then verify native V2 job visibility.
from videoroll.apps.subtitle_service.main import (
    _read_task_queue as py_queue_read, _download_queue_settings as py_queue_settings
)
from videoroll.apps.youtube_ingest.download_dispatch import (
    set_download_concurrency as py_set_downloads
)
from videoroll.apps.subtitle_service.schemas import TaskQueueSettingsRead

QUEUE_FIXTURES = [
  ("v1-render","00000000-0000-0000-0000-000000020101",10),
  ("v1-subtitle","00000000-0000-0000-0000-000000020102",8),
  ("v1-retry","00000000-0000-0000-0000-000000020103",6),
  ("v1-download","00000000-0000-0000-0000-000000020104",5),
  ("v2-download","00000000-0000-0000-0000-000000020105",9),
]
for name,task,priority in QUEUE_FIXTURES:
    execute("""INSERT INTO tasks(id,source_type,source_license,status,priority)
       VALUES(CAST(:task AS uuid),'local','own','created',:priority)""",
       {"task":task,"priority":priority})
render_job="00000000-0000-0000-0000-000000020106"
subtitle_job="00000000-0000-0000-0000-000000020107"
execute("""INSERT INTO render_jobs(id,task_id,status,progress)
           VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'queued',12)""",
       {"id":render_job,"task":QUEUE_FIXTURES[0][1]})
execute("""INSERT INTO subtitle_jobs(id,task_id,status,progress,lease_owner,lease_until)
           VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'running',42,'worker-test',now()+interval '10 minutes')""",
       {"id":subtitle_job,"task":QUEUE_FIXTURES[1][1]})
execute("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,request_json)
     VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'hatchet','VideoPipelineV1',
            'launch_failed','{}'::jsonb)""",
       {"id":"00000000-0000-0000-0000-000000020108","task":QUEUE_FIXTURES[2][1]})
execute("""INSERT INTO operation_inbox(id,operation_key,status,request_json)
       VALUES(CAST(:id AS uuid),:key,'pending','{}'::jsonb)""",
       {"id":"00000000-0000-0000-0000-000000020109",
        "key":"youtube-download:"+QUEUE_FIXTURES[3][1]})
with Session(engine) as session:
    py_queue=py_queue_read(session,limit=200).model_dump(mode="json")
    py_settings=TaskQueueSettingsRead(**py_queue_settings(session).model_dump()).model_dump()
status,rust_queue=request("GET","/subtitle/task_queue?limit=200")
assert status==200,(status,rust_queue)
ours={row["task_id"]:row for row in rust_queue["tasks"]}
for row in py_queue["tasks"]:
    r=ours[row["task_id"]]
    for field in ("task_id","state","stage","priority",
        "subtitle_job_id","render_job_id","progress","error_message"):
        assert row[field]==r[field],(field,row,r)
    assert abs(row["stage_elapsed_seconds"]-r["stage_elapsed_seconds"])<=4,(row,r)
for name,task,_ in QUEUE_FIXTURES[:4]:
    assert task in ours,(name,rust_queue)
for field in ("subtitle_worker_slots","download_concurrency","active_downloads","pending_downloads"):
    assert rust_queue["settings"][field]==py_settings[field],(field,rust_queue["settings"],py_settings)
assert rust_queue["settings"]["scheduler"] in ("hatchet","rust-v2")
# The Rust queue truthfully exposes its own scheduler after V2 tasks are present; the
# original Python Pydantic DTO hard-codes "hatchet", so this field intentionally differs.
print("PASS DIFF: original Python/Rust task queue states, real leases, workflow retry and download inbox")
# Python's update method writes the original workflow.youtube_download_concurrency
# rather than a V2 overlay; compare both DB side effect and API DTO.
saved=read("SELECT value_json,version FROM app_settings WHERE key='workflow.youtube_download_concurrency'")
with Session(engine) as session:
    py_set_downloads(session,4)
    py_queue_after=py_queue_settings(session).model_dump()
py_row=read("SELECT value_json,version FROM app_settings WHERE key='workflow.youtube_download_concurrency'")
if saved is None:
    execute("DELETE FROM app_settings WHERE key='workflow.youtube_download_concurrency'")
else:
    execute("""UPDATE app_settings SET value_json=CAST(:value AS jsonb), version=:version
         WHERE key='workflow.youtube_download_concurrency'""",
        {"value":json.dumps(saved["value_json"]),"version":saved["version"]})
status,rust_settings=request("PUT","/subtitle/task_queue/settings",{"max_concurrent":4})
assert status==200,(status,rust_settings)
for field in ("subtitle_worker_slots","download_concurrency","active_downloads","pending_downloads"):
    assert rust_settings[field]==py_queue_after[field],(field,rust_settings,py_queue_after)
assert rust_settings["scheduler"] in ("hatchet","rust-v2")
assert read("SELECT value_json,version FROM app_settings WHERE key='workflow.youtube_download_concurrency'")==py_row
assert request("PUT","/subtitle/task_queue/settings",{"max_concurrent":17})[0] in (400,422)
assert read("SELECT value_json,version FROM app_settings WHERE key='workflow.youtube_download_concurrency'")==py_row
print("PASS DIFF: Python/Rust simultaneous download setting and persistent transaction")
# V2-only active work must not disappear merely because Python has no Hatchet job.
v2_workflow="00000000-0000-0000-0000-000000020110"
v2_job="00000000-0000-0000-0000-000000020111"
execute("""UPDATE tasks SET runtime_owner='rust',runtime_origin='local_upload'
       WHERE id=CAST(:task AS uuid)""",{"task":QUEUE_FIXTURES[4][1]})
execute("""INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage)
       VALUES(CAST(:id AS uuid),CAST(:task AS uuid),1,'running','download')""",
       {"id":v2_workflow,"task":QUEUE_FIXTURES[4][1]})
execute("""INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,available_at)
       VALUES(CAST(:id AS uuid),CAST(:workflow AS uuid),CAST(:task AS uuid),
             'download',1,:key,'pending',now()+interval '10 years')""",{
       "id":v2_job,"workflow":v2_workflow,
       "task":QUEUE_FIXTURES[4][1],"key":"queue-diff-v2-download"})
status,with_v2=request("GET","/subtitle/task_queue?limit=200")
assert status==200,(status,with_v2)
v2_item=next(x for x in with_v2["tasks"] if x["task_id"]==QUEUE_FIXTURES[4][1])
assert v2_item["state"]=="queued" and v2_item["stage"]=="waiting_download",v2_item
assert with_v2["settings"]["scheduler"]=="rust-v2"
assert with_v2["settings"]["pending_downloads"]>=1
status,broker=request("GET","/subtitle/task_queue/broker")
assert status==200,(status,broker)
download=next(x for x in broker["queues"] if x["name"]=="v2:download")
assert download["ready"]>=0 and download["active"]>=0,download
assert broker["worker_observed"] is False
print("PASS: native V2 durable future download remains visible, with real broker counts")
execute("""UPDATE vr2_jobs SET state='running',lease_token=CAST(:token AS uuid),
            lease_until=now()+interval '5 minutes'
            WHERE id=CAST(:id AS uuid)""",
        {"id":v2_job,"token":"00000000-0000-0000-0000-000000020112"})
status,with_v2_running=request("GET","/subtitle/task_queue")
assert status==200,(status,with_v2_running)
v2_running=next(x for x in with_v2_running["tasks"] if x["task_id"]==QUEUE_FIXTURES[4][1])
assert v2_running["state"]=="running",v2_running
status,broker=request("GET","/subtitle/task_queue/broker")
download=next(x for x in broker["queues"] if x["name"]=="v2:download")
assert download["active"]>=1 and download["ready"]>=0,download
execute("""UPDATE vr2_jobs SET lease_until=now()-interval '1 minute'
        WHERE id=CAST(:id AS uuid)""",{"id":v2_job})
status,recovered=request("GET","/subtitle/task_queue")
recovered_item=next(x for x in recovered["tasks"] if x["task_id"]==QUEUE_FIXTURES[4][1])
assert recovered_item["state"]=="queued",recovered_item
print("PASS: expired V2 download lease is queued for recovery rather than missing or falsely running")
# Remove synthetic native jobs before the full smoke worker starts.
# Otherwise the worker may legitimately claim these intentionally incomplete
# fixtures, producing irrelevant retries and affecting later render claims.
execute("DELETE FROM vr2_jobs WHERE id=CAST(:id AS uuid)",{"id":v2_job})
execute("DELETE FROM vr2_workflows WHERE id=CAST(:id AS uuid)",{"id":v2_workflow})
execute("UPDATE tasks SET runtime_owner='python',runtime_origin=NULL WHERE id=CAST(:task AS uuid)",{
    "task":QUEUE_FIXTURES[4][1]})
execute("DELETE FROM tasks WHERE id=CAST(:task AS uuid)",{
    "task":QUEUE_FIXTURES[4][1]})
print("PASS: native scheduler queue fixtures cleaned before worker smoke")



# Manual translation context is a real mutating V1 API, not a cosmetic
# editor. Compare Python and Rust writes on two equal isolated task fixtures.
from pathlib import Path as _Path
from videoroll.apps.subtitle_service.main import (
    put_task_translation_context as py_put_context,
    get_task_translation_context as py_get_context
)
context_root=_Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
context_settings=SubtitleServiceSettings(
    _env_file=None,STORAGE_ROOT=str(context_root),REDIS_URL="redis://127.0.0.1:1/99")
CONTEXT_PY="00000000-0000-0000-0000-000000030101"
CONTEXT_RU="00000000-0000-0000-0000-000000030102"
CONTEXT_JOB_PY="00000000-0000-0000-0000-000000030103"
CONTEXT_JOB_RU="00000000-0000-0000-0000-000000030104"
source_segments=[
 {"start":0,"end":2,"text":"Alice was repairing the circuit."},
 {"start":2,"end":5,"text":"The PCB is damaged."},
 {"start":5,"end":7,"text":"The PCBA board is intact."}
]
old_memory={
 "characters":[{"name":"Alice","target_name":"爱丽丝"}],
 "terminology":[{"source":"PCB","target":"板"}]
}
for task,job in ((CONTEXT_PY,CONTEXT_JOB_PY),(CONTEXT_RU,CONTEXT_JOB_RU)):
    execute("""INSERT INTO tasks(id,source_type,source_license,status)
      VALUES(CAST(:task AS uuid),'local','own','created')""",{"task":task})
    execute("""INSERT INTO subtitle_jobs(id,task_id,status,request_json)
      VALUES(CAST(:job AS uuid),CAST(:task AS uuid),'queued',
      CAST(:payload AS jsonb))""",{"job":job,"task":task,"payload":json.dumps({
        "artifacts":{"translation_context_key":
            f"sub/{task}/translation_context/old.json"}
    })})
    previous=f"sub/{task}/translation_context/old.json"
    source=f"sub/{task}/raw/segments.json"
    for key,data in ((previous,{"summary":"previous", "memory":old_memory}),
                     (source,source_segments)):
        output=context_root/key
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
    execute("""INSERT INTO assets(id,task_id,kind,storage_key)
      VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'segments_json',:key)""",{
      "id":str(uuid.uuid4()),"task":task,"key":source})
context_payload={
 "summary":"Character and terminology correction",
 "memory":{
   "topic":"  Electronics  ",
   "characters":[{"name":"Alice","target_name":"艾丽斯","role":"technician"}],
   "terminology":[{"source":"PCB","target":"印刷电路板"}],
   "ambiguities":[{"term":"glue","resolution":"adhesive"}]
 }
}
with Session(engine) as session:
    py_changed=py_put_context(uuid.UUID(CONTEXT_PY),context_payload,
                              session,context_settings)
    py_read=py_get_context(uuid.UUID(CONTEXT_PY),session,context_settings)
status,ru_changed=request("PUT",f"/subtitle/tasks/{CONTEXT_RU}/translation-context",
                          context_payload)
assert status==200,(status,ru_changed)
for field in ("available","summary","memory","changed_terms","affected_indices"):
    assert ru_changed[field]==py_changed[field],(
        field,ru_changed.get(field),py_changed.get(field))
assert ru_changed["affected_indices"]==[1,2],ru_changed
assert ru_changed["key"].startswith(f"sub/{CONTEXT_RU}/translation_context/manual-")
assert ru_changed["job_id"]==CONTEXT_JOB_RU
assert json.loads((context_root/ru_changed["key"]).read_text())==json.loads(
    (context_root/py_changed["key"]).read_text())
read_status,ru_context=request(
    "GET",f"/subtitle/tasks/{CONTEXT_RU}/translation-context")
assert read_status==200,(read_status,ru_context)
for field in ("available","summary","memory","affected_indices"):
    assert ru_context[field]==py_read[field],(field,ru_context,py_read)
ru_pointer=read("""SELECT request_json->'artifacts'->>'translation_context_key' AS key
 FROM subtitle_jobs WHERE id=CAST(:job AS uuid)""",{"job":CONTEXT_JOB_RU})["key"]
assert ru_pointer==ru_changed["key"]
print("PASS DIFF: Python/Rust manual translation context, changed terms, exact affected cue indices, artifact bytes and GET readback")
bad_pointer=ru_pointer
code,_=request("PUT",f"/subtitle/tasks/{CONTEXT_RU}/translation-context",
               {"summary":"reject empty","memory":{}})
assert code in (400,422)
assert read("""SELECT request_json->'artifacts'->>'translation_context_key' AS key
 FROM subtitle_jobs WHERE id=CAST(:job AS uuid)""",{"job":CONTEXT_JOB_RU})["key"]==bad_pointer
assert request("PUT",f"/subtitle/tasks/{uuid.uuid4()}/translation-context",
               context_payload)[0]==404
print("PASS DIFF: empty/missing manual translation context write rejected with no pointer mutation")


# Whisper model filesystem management: same named folders, nested byte size,
# hidden temp directory handling, and actual deletion, using only test root.
from videoroll.apps.subtitle_service.main import (
    list_whisper_models as py_list_models, delete_whisper_model as py_delete_model
)
model_root=_Path(os.environ["SUBTITLE_WHISPER_MODEL_DIR"])
model_root.mkdir(parents=True,exist_ok=True)
for name in ("whisper-tiny", "whisper-large-v3"):
    folder=model_root/name
    folder.mkdir(parents=True,exist_ok=True)
    (folder/"config.json").write_text('{"model":"test"}',encoding="utf-8")
    (folder/"nested").mkdir(exist_ok=True)
    (folder/"nested"/"weights.bin").write_bytes(bytes(range(256)))
hidden=model_root/".hidden-download-partial"
hidden.mkdir(exist_ok=True)
(hidden/"partial.bin").write_bytes(b"not listed")
with Session(engine) as session:
    expected_models=[v.model_dump() for v in py_list_models(context_settings)]
status,got_models=request("GET","/subtitle/models")
assert status==200,(status,got_models)
assert got_models==expected_models,(got_models,expected_models)
assert all(not item["name"].startswith(".") for item in got_models)
assert got_models[0]["size_bytes"]>256
print("PASS DIFF: Python/Rust disk-backed Whisper model directory names, paths, hidden folders and recursive bytes")
for name in ("py-delete-check","ru-delete-check"):
    folder=model_root/name
    folder.mkdir(exist_ok=True)
    (folder/"model.bin").write_bytes(b"delete-this-fixture")
python_deleted=py_delete_model("py-delete-check",context_settings)
status,ru_deleted=request("DELETE","/subtitle/models/ru-delete-check")
assert status==200 and ru_deleted==python_deleted,(status,ru_deleted,python_deleted)
assert not (model_root/"py-delete-check").exists()
assert not (model_root/"ru-delete-check").exists()
assert request("DELETE","/subtitle/models/nonexistent-test-model")[0]==404
assert request("DELETE","/subtitle/models/bad%20name")[0] in (400,422)
print("PASS DIFF: real Whisper model directory deletion, missing/invalid names, no residual files")


# Real HuggingFace proxy probe, with a disposable loopback *forward proxy*.
# Both the Python original (httpx) and Rust (reqwest) must traverse HTTP proxy.
from videoroll.apps.subtitle_service.main import test_model_download_proxy as py_proxy_test
from videoroll.apps.subtitle_service.schemas import ModelDownloadProxyTestRequest
for url,expected_status in (
    ("http://huggingface.co/robots.txt",200),
    ("http://huggingface.co/blocked",403)
):
    body={"proxy":"http://127.0.0.1:18089","url":url}
    with Session(engine) as session:
        py_probe=py_proxy_test(ModelDownloadProxyTestRequest(**body),
            context_settings,session).model_dump()
    status,rust_probe=request("POST","/subtitle/models/proxy/test",body)
    assert status==200,(status,rust_probe)
    for field in ("ok","url","used_proxy","status_code","error"):
        assert rust_probe[field]==py_probe[field],(field,rust_probe,py_probe)
    assert rust_probe["status_code"]==expected_status,rust_probe
    assert rust_probe["elapsed_ms"]>=0 and py_probe["elapsed_ms"]>=0
print("PASS DIFF: Python/Rust model proxy makes real HTTP through isolated forward proxy, reports 200 and 403")
# The saved ASR model_download_proxy must be read when the payload omits proxy.
code,_=request("PUT","/subtitle/asr/settings",
    {"model_download_proxy":"http://127.0.0.1:18089"})
assert code==200,code
with Session(engine) as session:
    py_saved=py_proxy_test(ModelDownloadProxyTestRequest(
       url="http://huggingface.co/robots.txt"),context_settings,session).model_dump()
status,ru_saved=request("POST","/subtitle/models/proxy/test",
    {"url":"http://huggingface.co/robots.txt"})
assert status==200,(status,ru_saved)
for field in ("ok","url","used_proxy","status_code","error"):
    assert ru_saved[field]==py_saved[field],(field,ru_saved,py_saved)
assert ru_saved["used_proxy"]=="http://127.0.0.1:18089",ru_saved
print("PASS DIFF: model proxy test reads actual saved Python subtitle.asr setting")
for invalid in (
    "http://127.0.0.1/admin",
    "https://huggingface.co.attacker.test/exfil",
    "https://someone@huggingface.co/robots.txt"
):
    with Session(engine) as session:
        py_bad=py_proxy_test(ModelDownloadProxyTestRequest(
            url=invalid,proxy="http://127.0.0.1:18089"),
            context_settings,session).model_dump()
    code,ru_bad=request("POST","/subtitle/models/proxy/test",
        {"url":invalid,"proxy":"http://127.0.0.1:18089"})
    assert code==200,(code,ru_bad)
    for field in ("ok","url","used_proxy","status_code","elapsed_ms","error"):
        assert py_bad[field]==ru_bad[field],(field,py_bad,ru_bad)
print("PASS DIFF: invalid model proxy URL returns exact Python response without network request")


# ZIP64-capable Whisper ZIP upload: Rust owns multipart and native ZIP decoding;
# Python's original ZIP extractor is used only as the differential oracle.
# Compare actual files and directory names against the original Python API.
import io,asyncio,zipfile
from urllib.parse import quote
from starlette.datastructures import UploadFile
from videoroll.apps.subtitle_service.main import upload_whisper_model as py_upload_model
blob_io=io.BytesIO()
with zipfile.ZipFile(blob_io,"w",compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("config.json",'{"model":"whisper-test"}')
    archive.writestr("weights/model.bin",bytes(range(256))*4)
blob=blob_io.getvalue()
def model_multipart(name:str,content:bytes,filename="model.zip"):
    boundary="----videoroll-rust-fixture-e7"
    data=(
      f"--{boundary}\r\n"
      f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
      "Content-Type: application/zip\r\n\r\n").encode()+content+(
      f"\r\n--{boundary}--\r\n").encode()
    hdr={
        "x-videoroll-v2-internal-token":TOKEN,
        "content-type":f"multipart/form-data; boundary={boundary}"}
    req=Request(BASE+"/subtitle/models/upload?name="+quote(name),method="POST",
                headers=hdr,data=data)
    try:
        with urlopen(req,timeout=25) as r:
            return r.status,json.loads(r.read())
    except HTTPError as e:
        return e.code,json.loads(e.read())
py_file=UploadFile(file=io.BytesIO(blob),filename="whisper-demo.zip")
py_result=asyncio.run(py_upload_model("python-model-zip",py_file,context_settings)).model_dump()
code,ru_result=model_multipart("rust-model-zip",blob)
assert code==200,(code,ru_result)
assert ru_result["name"]=="rust-model-zip" and ru_result["size_bytes"]==py_result["size_bytes"],(py_result,ru_result)
def archive_tree(name:str):
    root=model_root/name
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob("*") if p.is_file()}
assert archive_tree("python-model-zip")==archive_tree("rust-model-zip")
assert len(archive_tree("rust-model-zip"))==2
assert request("GET","/subtitle/models")[0]==200
print("PASS DIFF: Python/Rust actual ZIP upload creates identical model directory and decompressed weights")
# Force ZIP64 local headers even though the isolated fixture is small.
# This exercises the real model archive format without allocating gigabytes.
zip64_io=io.BytesIO()
with zipfile.ZipFile(zip64_io,"w",allowZip64=True) as zf:
    with zf.open("large.bin","w",force_zip64=True) as output:
        output.write(bytes(range(256))*3)
zip64_blob=zip64_io.getvalue()
py_zip64=asyncio.run(py_upload_model("python-model-zip64",
    UploadFile(file=io.BytesIO(zip64_blob),filename="zip64.zip"),context_settings)).model_dump()
zip64_code,ru_zip64=model_multipart("rust-model-zip64",zip64_blob)
assert zip64_code==200,(zip64_code,ru_zip64)
assert ru_zip64["size_bytes"]==py_zip64["size_bytes"]
assert archive_tree("python-model-zip64")==archive_tree("rust-model-zip64")
print("PASS DIFF: real ZIP64 local header streams decode identically in Python and native Rust")

assert model_multipart("rust-model-zip",blob)[0] in (400,409)
assert model_multipart("invalid name",blob)[0] in (400,422)
assert model_multipart("wrong-extension",blob,"weights.tar")[0] in (400,422)
unsafe=io.BytesIO()
with zipfile.ZipFile(unsafe,"w") as zf:
    zf.writestr("../outside-of-root.txt","deny")
assert model_multipart("untrusted-model",unsafe.getvalue())[0] in (400,422)
assert not (model_root/"untrusted-model").exists()
assert not (model_root.parent/"outside-of-root.txt").exists()
assert not list(model_root.glob(".model-upload-*.zip"))
assert not list(model_root.glob(".*.extract-*"))
print("PASS DIFF: duplicate, invalid filename, traversal ZIP all rejected; staging files cleaned")


# Real Rust Hugging Face Hub snapshot: manifest API + file streams, with no
# Python huggingface_hub dependency in the Rust process. Python's original
# download_model_snapshot is invoked against a mocked snapshot_download
# implementation as the same-directory/filename semantics oracle.
from unittest.mock import patch
from videoroll.apps.subtitle_service.model_downloads import (
    download_model_snapshot as py_snapshot_download,
    default_model_dir_name as py_default_model_name,
    resolve_model_repo_id as py_repo_id
)
assert py_repo_id("faster-whisper","tiny")=="Systran/faster-whisper-tiny"
assert py_default_model_name("openvino","base")=="whisper-base-fp16-ov"
assert py_default_model_name("embedding","bge-m3")=="bge-m3"
assert request("PUT","/subtitle/asr/settings",{"model_download_proxy":""})[0]==200
assert py_default_model_name("faster-whisper","tiny")=="tiny"
def mock_snapshot_download(repo_id,revision,local_dir):
    assert repo_id=="Systran/faster-whisper-tiny",(repo_id,revision)
    assert revision in ("main","v2"),revision
    destination=_Path(local_dir)
    destination.mkdir(parents=True,exist_ok=True)
    config=(b'{"model":"tiny","revision":"main"}' if revision=="main"
           else b'{"model":"tiny","revision":"v2"}')
    weights=(bytes(range(256))*4 if revision=="main"
             else b"model-v2-weights-updated")
    for filename,data in (("config.json",config),
                          ("weights/model.bin",weights)):
        target=destination/filename
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
    return str(destination)
import sys,types
hub_stub=types.ModuleType("huggingface_hub")
hub_stub.snapshot_download=mock_snapshot_download
with patch.dict(sys.modules,{"huggingface_hub":hub_stub}):
    py_model=py_snapshot_download(
        engine="faster-whisper",model="tiny",model_dir=model_root,
        name="python-downloaded-tiny",revision="main")
code,rs_model=request("POST","/subtitle/models/download",
    {"engine":"faster-whisper","model":"tiny","revision":"main"})
assert code==200,(code,rs_model)
assert rs_model["name"]=="tiny" and rs_model["path"]==str(model_root/"tiny"),rs_model
assert rs_model["size_bytes"]==sum(p.stat().st_size for p in (model_root/"tiny").rglob("*") if p.is_file())
assert archive_tree("tiny")==archive_tree("python-downloaded-tiny")
assert len(archive_tree("tiny"))==2
print("PASS DIFF: native Hugging Face snapshot manifest, streaming model blobs, default repo/name map and Python on-disk output")
# An existing model cannot be silently overwritten without explicit force.
code,_=request("POST","/subtitle/models/download",
     {"engine":"faster-whisper","model":"tiny","revision":"v2"})
assert code==502 and archive_tree("tiny")["config.json"].find(b"main")>=0
with patch.dict(sys.modules,{"huggingface_hub":hub_stub}):
    py_updated=py_snapshot_download(
        engine="faster-whisper",model="tiny",model_dir=model_root,
        name="python-downloaded-tiny",revision="v2",force=True)
code,rs_updated=request("POST","/subtitle/models/download",
     {"engine":"faster-whisper","model":"tiny","revision":"v2","force":True})
assert code==200,(code,rs_updated)
assert archive_tree("tiny")==archive_tree("python-downloaded-tiny")
assert archive_tree("tiny")["config.json"].find(b"v2")>=0
assert not list(model_root.glob(".tiny.downloading-*"))
assert not list(model_root.glob(".tiny.old-*"))
print("PASS DIFF: Rust force replacement atomically installs new revision and matches Python final model bytes")
# A missing revision must not destroy an existing model during a forced update.
before=archive_tree("tiny")
code,miss=request("POST","/subtitle/models/download",
     {"engine":"faster-whisper","model":"tiny","revision":"unknown","force":True})
assert code==502,(code,miss)
assert archive_tree("tiny")==before
assert not list(model_root.glob(".tiny.downloading-*"))
print("PASS: failed Hugging Face update retains old model and cleans staging")
for payload in (
    {"engine":"invalid","model":"tiny"},
    {"engine":"faster-whisper","model":""},
    {"engine":"faster-whisper","model":"../bad"},
    {"engine":"faster-whisper","model":"tiny","name":"../bad"},
):
    code,_=request("POST","/subtitle/models/download",payload)
    assert code in (400,422),(code,payload)
print("PASS: invalid model engines, repository traversal and destination names reject before network")


# ASR connection tests: actual generated 1s WAV and provider HTTP requests.
# Both references speak to an isolated HTTP server; no real Groq/Cloudflare
# account or production Whisper backend is touched.
from unittest.mock import patch as _patch
from videoroll.apps.subtitle_service import main as py_asr_main
from videoroll.apps.subtitle_service import processing as py_asr_processing
from videoroll.apps.subtitle_service.schemas import (
    ExternalWhisperTestRequest,GroqWhisperTestRequest,CloudflareWorkersAITestRequest)
ASR_MOCK="http://127.0.0.1:18091"
for key,secret in (
    ("external_whisper_api_key","external-fixture"),
    ("groq_whisper_api_key","groq-fixture"),
    ("cloudflare_workers_ai_api_key","cf-fixture")
):
    status,_=request("PUT","/subtitle/asr/settings",{key:secret})
    assert status==200,(key,status)
external_body={
 "base_url":ASR_MOCK+"/v1","model":"whisper-test",
 "batch_size":3,"vad_enabled":False,"speech_pad_ms":0,"vad_threshold":0.6
}
with Session(engine) as session:
    py_ext=py_asr_main.test_external_whisper(
        ExternalWhisperTestRequest(**external_body),context_settings,session).model_dump()
code,ru_ext=request("POST","/subtitle/asr/external/test",external_body)
assert code==200,(code,ru_ext)
for field in ("ok","status_code","text","segments","longest_segment_seconds","longest_segment_chars","error"):
    assert py_ext[field]==ru_ext[field],(field,py_ext,ru_ext)
assert ru_ext["ok"] and ru_ext["segments"]==1 and ru_ext["text"]=="External test success",ru_ext
print("PASS DIFF: external Whisper live multipart WAV, DB-stored key, normalized segments and full DTO match Python")
# Groq's original API URL is fixed to api.groq.com, so redirect *only* the
# Python reference call to the local mock. Rust uses the loopback test override.
groq_body={"model":"whisper-large-v3-turbo"}
_real_groq=py_asr_main.transcribe_groq_whisper
def local_groq(audio_path,**kwargs):
    return _real_groq(audio_path,base_url=ASR_MOCK+"/groq/v1",**kwargs)
with _patch.object(py_asr_main,"transcribe_groq_whisper",side_effect=local_groq):
    with Session(engine) as session:
        py_groq=py_asr_main.test_groq_whisper(
            GroqWhisperTestRequest(**groq_body),context_settings,session).model_dump()
code,ru_groq=request("POST","/subtitle/asr/groq/test",groq_body)
assert code==200,(code,ru_groq)
for field in ("ok","status_code","text","segments","error"):
    assert py_groq[field]==ru_groq[field],(field,py_groq,ru_groq)
assert ru_groq["ok"] and ru_groq["text"]=="Groq test success",ru_groq
print("PASS DIFF: Groq multipart FLAC conversion and native HTTP transcription match original Python")
# CF's original endpoint is fixed. Route only the original Python httpx.post
# call to the isolated provider, preserving the JSON WAV/base64 payload.
cf_body={"account_id":"testacct","model":"@cf/openai/whisper-large-v3-turbo"}
_original_post=py_asr_processing.httpx.post
def local_cloudflare(url,*args,**kwargs):
    if url.startswith("https://api.cloudflare.com/"):
        url=ASR_MOCK+url[len("https://api.cloudflare.com"):]
    return _original_post(url,*args,**kwargs)
with _patch.object(py_asr_processing.httpx,"post",side_effect=local_cloudflare):
    with Session(engine) as session:
        py_cf=py_asr_main.test_cloudflare_workers_ai(
           CloudflareWorkersAITestRequest(**cf_body),context_settings,session).model_dump()
code,ru_cf=request("POST","/subtitle/asr/cloudflare/test",cf_body)
assert code==200,(code,ru_cf)
for field in ("ok","status_code","text","segments","error"):
    assert py_cf[field]==ru_cf[field],(field,py_cf,ru_cf)
assert ru_cf["ok"] and ru_cf["text"]=="Cloudflare test success",ru_cf
print("PASS DIFF: Cloudflare Workers AI real base64 WAV POST, stored token, timed segments and Python response")
for endpoint,bad_payload in (
    ("/subtitle/asr/groq/test",{"model":"unsupported"}),
    ("/subtitle/asr/cloudflare/test",{"account_id":"bad/account","model":"@cf/openai/whisper-large-v3-turbo"}),
    ("/subtitle/asr/cloudflare/test",{"account_id":"testacct","model":"not-cloudflare"}),
):
    code,rejected=request("POST",endpoint,bad_payload)
    assert code==200 and rejected["ok"] is False and rejected["error"],(code,rejected)
print("PASS DIFF: provider validation errors remain non-success ASR test results")


# Dictionary CRUD and lookup are backed by the same original PostgreSQL
# schema, not a V2-only shadow. Compare original Python code and Rust HTTP
# using paired equal records. Exercise write side effects and search order.
from videoroll.apps.subtitle_service import dictionaries as py_dict
DICT_SOURCE_PY=uuid.UUID("00000000-0000-0000-0000-000000040101")
DICT_SOURCE_RU=uuid.UUID("00000000-0000-0000-0000-000000040102")
DICT_ENTRY_PY=uuid.UUID("00000000-0000-0000-0000-000000040103")
DICT_ENTRY_RU=uuid.UUID("00000000-0000-0000-0000-000000040104")
DICT_SECOND_PY=uuid.UUID("00000000-0000-0000-0000-000000040105")
DICT_SECOND_RU=uuid.UUID("00000000-0000-0000-0000-000000040106")
for source,entry,second,label in (
    (DICT_SOURCE_PY,DICT_ENTRY_PY,DICT_SECOND_PY,"python"),
    (DICT_SOURCE_RU,DICT_ENTRY_RU,DICT_SECOND_RU,"rust")
):
    execute("""INSERT INTO translation_dictionary_sources
      (id,name,slug,description,source_lang,target_lang,format,license,source_url,
       attribution,domain,priority,enabled,metadata,entry_count)
       VALUES(CAST(:id AS uuid),'Circuit Dictionary',:slug,'Initial test glossary',
       'en','zh','csv','MIT','https://example.org/dict',
       'Test Authors',:domain,25,true,
       CAST(:metadata AS jsonb),2)""",
       {"id":source,"slug":f"test-dict-{label}","domain":f"electronics-{label}",
        "metadata":json.dumps({"isolated":True,"revision":1})})
    for id,term,norm,translation,quality in (
       (entry,"PCB","pcb","印刷电路板",0.95),
       (second,"PCB trace","pcb trace","电路板走线",0.82)
    ):
        execute("""INSERT INTO translation_dictionary_entries
           (id,source_id,source_lang,target_lang,term,normalized_term,
            translations,translation_text,definition,domain,quality,
            tags,aliases,examples,metadata,usage_count)
          VALUES(CAST(:id AS uuid),CAST(:source AS uuid),
            'en','zh',:term,:norm,CAST(:translations AS jsonb),
            :translation,'Electronics term',:domain,:quality,
            '["pcb","electronics"]'::jsonb,'["printed circuit board"]'::jsonb,
            '[{"source":"PCB trace","target":"走线"}]'::jsonb,
            '{"kind":"glossary"}'::jsonb,0)""",
            {"id":id,"source":source,"term":term,"norm":norm,
             "translation":translation,"translations":json.dumps([translation],ensure_ascii=False),
             "quality":quality,"domain":f"electronics-{label}"})
def dict_clean(value):
    return {k:v for k,v in value.items()
          if k not in ("id","source_id","slug","source_slug","domain","created_at","updated_at")}
orig_presets=py_dict.dictionary_import_presets()
code,ru_presets=request("GET","/subtitle/dictionaries/import-presets")
assert code==200 and ru_presets==orig_presets,(code,ru_presets,orig_presets)
print("PASS DIFF: Python/Rust dictionary import preset catalog identical")
with Session(engine) as s:
    py_sources=py_dict.list_dictionary_sources(s,q="Circuit",enabled=True)
code,ru_sources=request("GET","/subtitle/dictionaries/sources?q=Circuit&enabled=true")
assert code==200
py_source=next(x for x in py_sources if x["id"]==str(DICT_SOURCE_PY))
ru_source=next(x for x in ru_sources if x["id"]==str(DICT_SOURCE_RU))
assert dict_clean(py_source)==dict_clean(ru_source),(py_source,ru_source)
with Session(engine) as s:
    py_entries=py_dict.list_dictionary_entries(s,source_id=str(DICT_SOURCE_PY),
                                               q="PCB",limit=10,enabled=True)
code,ru_entries=request("GET",f"/subtitle/dictionaries/entries?source_id={DICT_SOURCE_RU}&q=PCB&enabled=true")
assert code==200 and len(py_entries)==len(ru_entries)==2,(code,py_entries,ru_entries)
for a,b in zip(py_entries,ru_entries):
    assert dict_clean(a)==dict_clean(b),(dict_clean(a),dict_clean(b))
assert sorted(x["term"] for x in ru_entries)==["PCB","PCB trace"]
print("PASS DIFF: original Python/Rust dictionary source and entry filters, priority, JSON projections and pagination")
src_update={"enabled":False,"priority":45,"domain":"Semiconductors","description":"Updated source"}
with Session(engine) as s:
    py_src_update=py_dict.update_dictionary_source(s,str(DICT_SOURCE_PY),**src_update)
    s.commit()
code,ru_src_update=request("PUT",f"/subtitle/dictionaries/sources/{DICT_SOURCE_RU}",src_update)
assert code==200,(code,ru_src_update)
assert dict_clean(py_src_update)==dict_clean(ru_src_update),(py_src_update,ru_src_update)
state=read("SELECT enabled,priority,domain,description FROM translation_dictionary_sources WHERE id=:id",
           {"id":DICT_SOURCE_RU})
assert state["enabled"] is False and state["priority"]==45 and state["domain"]=="Semiconductors"
with Session(engine) as s:
    py_nohits=py_dict.lookup_dictionary_entries(s,term="PCB",target_lang="zh",source_lang="en",domain="electronics-python")
    s.commit()
code,ru_nohits=request("POST","/subtitle/dictionaries/lookup",
          {"term":"PCB","target_lang":"zh","source_lang":"en","domain":"electronics-rust"})
assert code==200 and ru_nohits["count"]==0 and not py_nohits,(code,ru_nohits,py_nohits)
print("PASS DIFF: disabled source removes terms from live dictionary lookup")
with Session(engine) as s:
    py_dict.update_dictionary_source(s,str(DICT_SOURCE_PY),enabled=True)
    s.commit()
assert request("PUT",f"/subtitle/dictionaries/sources/{DICT_SOURCE_RU}",{"enabled":True})[0]==200
with Session(engine) as s:
    py_hit=py_dict.lookup_dictionary_entries(
        s,term="PCB",target_lang="zh",source_lang="en",limit=8,exact=True,domain="electronics-python")
    s.commit()
code,ru_hit=request("POST","/subtitle/dictionaries/lookup",
                     {"term":"PCB","target_lang":"zh","source_lang":"en","domain":"electronics-rust"})
assert code==200 and ru_hit["count"]==len(py_hit)==1,(code,ru_hit,py_hit)
assert dict_clean(ru_hit["results"][0])==dict_clean(py_hit[0]),(ru_hit,py_hit)
usage_py=read("SELECT usage_count,last_lookup_at FROM translation_dictionary_entries WHERE id=:id",
              {"id":DICT_ENTRY_PY})
usage_ru=read("SELECT usage_count,last_lookup_at FROM translation_dictionary_entries WHERE id=:id",
              {"id":DICT_ENTRY_RU})
assert usage_py["usage_count"]==usage_ru["usage_count"]==1
assert usage_py["last_lookup_at"] is not None and usage_ru["last_lookup_at"] is not None
with Session(engine) as s:
    py_partial=py_dict.lookup_dictionary_entries(s,term="PCB",target_lang="zh",source_lang="en",exact=False,domain="electronics-python")
    s.commit()
code,ru_partial=request("POST","/subtitle/dictionaries/lookup",
                        {"term":"PCB","source_lang":"en","exact":False,"domain":"electronics-rust"})
assert code==200 and ru_partial["count"]==len(py_partial)==2
assert [x["term"] for x in ru_partial["results"]]==[x["term"] for x in py_partial]
assert all(dict_clean(a)==dict_clean(b) for a,b in zip(py_partial,ru_partial["results"]))
print("PASS DIFF: exact/fuzzy lookup order and transactional usage_count + last_lookup_at persistence")
with Session(engine) as s:
    py_disabled=py_dict.set_dictionary_entry_enabled(s,str(DICT_ENTRY_PY),False)
    s.commit()
code,ru_disabled=request("PUT",f"/subtitle/dictionaries/entries/{DICT_ENTRY_RU}",{"enabled":False})
assert code==200 and dict_clean(py_disabled)==dict_clean(ru_disabled),(code,py_disabled,ru_disabled)
with Session(engine) as s:
    py_nohit=py_dict.lookup_dictionary_entries(s,term="PCB",target_lang="zh",exact=True,domain="electronics-python")
    s.commit()
code,ru_nohit=request("POST","/subtitle/dictionaries/lookup",{"term":"PCB","domain":"electronics-rust"})
assert code==200 and not py_nohit and ru_nohit["results"]==[],(code,ru_nohit)
print("PASS DIFF: disabling one entry excludes it from exact lookup, preserves original metadata")
with Session(engine) as s:
    py_deleted=py_dict.delete_dictionary_entry(s,str(DICT_ENTRY_PY))
    s.commit()
code,ru_deleted=request("DELETE",f"/subtitle/dictionaries/entries/{DICT_ENTRY_RU}")
assert code==200 and py_deleted and ru_deleted=={"deleted":True},(code,ru_deleted)
assert request("DELETE",f"/subtitle/dictionaries/entries/{DICT_ENTRY_RU}")[0]==404
with Session(engine) as s:
    py_deleted=py_dict.delete_dictionary_source(s,str(DICT_SOURCE_PY))
    s.commit()
code,ru_deleted=request("DELETE",f"/subtitle/dictionaries/sources/{DICT_SOURCE_RU}")
assert code==200 and py_deleted and ru_deleted=={"deleted":True}
assert read("SELECT count(*) AS n FROM translation_dictionary_entries WHERE source_id=:id",
            {"id":DICT_SOURCE_RU})["n"]==0
assert request("DELETE",f"/subtitle/dictionaries/sources/{DICT_SOURCE_RU}")[0]==404
print("PASS DIFF: dictionary entry delete, cascading source deletion and 404 on missing resource")

print("PASS: Python/Rust same-fixture differential completed; no production data accessed")
