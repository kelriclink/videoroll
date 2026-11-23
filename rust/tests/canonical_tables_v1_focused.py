#!/usr/bin/env python3
"""Original-table-only verification on a disposable PostgreSQL fixture."""
import json,os,uuid
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from videoroll.db.models import AppSetting,AlertEvent
url=os.environ["DATABASE_URL"].replace("postgresql://","postgresql+psycopg://",1)
assert url.endswith("/videoroll_test"),"refusing non-isolated database"
engine=create_engine(url)
api="http://127.0.0.1:18080"
secret=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
def request(method,path,body=None):
    h={"x-videoroll-v2-internal-token":secret}
    data=None
    if body is not None:
        h["content-type"]="application/json"
        data=json.dumps(body,ensure_ascii=False).encode("utf-8")
    req=Request(api+path,data=data,headers=h,method=method)
    try: response=urlopen(req,timeout=12)
    except HTTPError as e: response=e
    with response:
        raw=response.read()
        return response.status,json.loads(raw) if raw else None
def fetch_one(sql,**params):
    with engine.connect() as c:return c.execute(text(sql),params).scalar()
for name in ("vr2_settings","vr2_alerts"):
    assert fetch_one("SELECT to_regclass(:name)",name="public."+name) is None,name
assert fetch_one("SELECT to_regclass('public.app_settings')") is not None
assert fetch_one("SELECT to_regclass('public.alert_events')") is not None
print("PASS: duplicate settings/alerts relations removed, only canonical Python tables remain")
assert fetch_one("SELECT to_regclass('public.vr2_managed_tasks')") is None
status,managed=request("POST","/api/v1/tasks",{"source_type":"local","source_license":"own","created_by":"schema-unified-test"})
assert status==201,(status,managed)
owned=fetch_one("SELECT runtime_owner FROM tasks WHERE id=CAST(:id AS uuid)",id=managed["id"])
origin=fetch_one("SELECT runtime_origin FROM tasks WHERE id=CAST(:id AS uuid)",id=managed["id"])
assert owned=="rust" and origin=="local_upload",(owned,origin)
with engine.begin() as conn:
    conn.execute(text("INSERT INTO tasks(id,source_type,source_license,status) VALUES(CAST(:id AS uuid),'local','own','created')"),{"id":str(uuid.uuid4())})
assert fetch_one("SELECT count(*) FROM tasks WHERE runtime_owner='python'")>=1
print("PASS: original tasks table distinguishes Python/Rust ownership without duplicate ownership table")


key="subtitle.auto_profile"
with Session(engine) as db:
    row=db.get(AppSetting,key)
    if row is None:
        row=AppSetting(key=key,value_json={"python_owned_setting":"preserve","translate_enabled":False},version=5)
        db.add(row)
    else:
        row.value_json={**row.value_json,"python_owned_setting":"preserve","translate_enabled":False}
        row.version=5
    db.commit()
status,initial=request("GET","/api/v2/settings/auto")
assert status==200 and initial["source"]=="app_settings" and initial["version"]==5,initial
assert initial["value"]["python_owned_setting"]=="preserve",initial
status,patched=request("PUT","/api/v2/settings/auto",{
    "expected_version":5,"value":{"auto_publish":False,"asr_language":"ja"}
})
assert status==200 and patched["version"]==6,(status,patched)
assert patched["value"]["python_owned_setting"]=="preserve",patched
with Session(engine) as db:
    row=db.get(AppSetting,key)
    assert row.version==6 and row.value_json["asr_language"]=="ja"
    assert row.value_json["python_owned_setting"]=="preserve"
    row.value_json={**row.value_json,"asr_language":"en"}
    row.version+=1
    db.commit()
status,reread=request("GET","/api/v2/settings/auto")
assert status==200 and reread["value"]["asr_language"]=="en" and reread["version"]==7,reread
assert request("PUT","/api/v2/settings/auto",{
    "expected_version":6,"value":{"auto_publish":True}
})[0]==409
print("PASS: Python->Rust and Rust->Python settings immediate visibility, merge and stale-version fencing")

name="canonical-rust-verified-"+uuid.uuid4().hex
status,report=request("POST","/api/v1/operations/alerts/report",{
    "fingerprint":name,"source":"rust-schema-test","severity":"warning",
    "title":"共享告警表","message":"Python/Rust 可见",
    "details":{"task_id":str(uuid.uuid4()),"component":"rust"}
})
assert status==200,(status,report)
assert report["fingerprint"]==name and report["details"]["component"]=="rust",report
alert_id=report["id"]
with Session(engine) as db:
    row=db.query(AlertEvent).filter(AlertEvent.fingerprint==name).one()
    assert str(row.id)==alert_id and row.message=="Python/Rust 可见"
    assert row.details_json["component"]=="rust"
    row.message="Python updated this row"
    db.commit()
status,rows=request("GET","/api/v1/operations/alerts?limit=200")
assert status==200 and any(x["fingerprint"]==name and
    x["message"]=="Python updated this row" for x in rows),(status,rows)
status,acked=request("POST",f"/api/v1/operations/alerts/{alert_id}/ack",{})
assert status==200 and acked["status"]=="acknowledged",(status,acked)
with Session(engine) as db:
    assert db.get(AlertEvent,uuid.UUID(alert_id)).status=="acknowledged"
status,resolved=request("POST",f"/api/v1/operations/alerts/{alert_id}/resolve",{})
assert status==200 and resolved["status"]=="resolved",(status,resolved)
with Session(engine) as db:
    assert db.get(AlertEvent,uuid.UUID(alert_id)).status=="resolved"
print("PASS: canonical alert_events Python/Rust shared CRUD, status, original JSON field and no double record")

status,connection=request("PUT","/api/v1/render-management/connection",{
    "server_url":"https://original-worker.example.test/"
})
assert status==200,(status,connection)
with Session(engine) as db:
    row=db.get(AppSetting,"render_worker_connection")
    assert row is not None and row.value_json["server_url"]=="https://original-worker.example.test"
print("PASS: worker connection configuration also uses original app_settings")

# Migrations must not discard conflicting values. Verify on this same
# disposable database; a failed transaction automatically removes its
# temporarily recreated shadow table.
from pathlib import Path
migration=Path(__file__).resolve().parents[1]/"migrations"/"20261123000019_unify_settings_alerts.sql"
sql=migration.read_text()
shadow="""CREATE TABLE vr2_settings(
    key VARCHAR(128) PRIMARY KEY,
    value_json JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""
try:
    with engine.begin() as conn:
        conn.execute(text(shadow))
        conn.execute(text("""INSERT INTO vr2_settings(key,value_json)
            VALUES('subtitle.auto_profile','{"conflicting":"must-not-overwrite"}'::jsonb)"""))
        conn.execute(text(sql))
    raise AssertionError("migration improperly overwrote original settings")
except Exception as e:
    if isinstance(e,AssertionError):raise
    assert "conflicting legacy and V2 settings" in str(e),str(e)
assert fetch_one("SELECT to_regclass('public.vr2_settings')") is None
with Session(engine) as db:
    row=db.get(AppSetting,"subtitle.auto_profile")
    assert row is not None and row.value_json["python_owned_setting"]=="preserve"
with engine.begin() as conn:
    conn.execute(text(shadow))
    conn.execute(text("""INSERT INTO vr2_settings(key,value_json)
        VALUES('schema.unification.synthetic','{"migration":"preserved"}'::jsonb)"""))
    conn.execute(text(sql))
assert fetch_one("SELECT to_regclass('public.vr2_settings')") is None
with Session(engine) as db:
    assert db.get(AppSetting,"schema.unification.synthetic").value_json=={"migration":"preserved"}
print("PASS: divergent legacy/V2 config migration aborts without loss; nonconflicting rows carry over")

# Original Python task titles and Rust metadata/title UI share the one
# original task.title.<uuid> setting without a shadow title table.
from videoroll.apps.subtitle_service.task_title_store import set_task_titles,get_task_titles
assert fetch_one("SELECT to_regclass('public.vr2_task_titles')") is None
with Session(engine) as db:
    set_task_titles(db,managed["id"],source_title="源视频标题",translated_title="统一后的中文标题")
status,task_view=request("GET",f"/api/v1/tasks/{managed['id']}")
assert status==200 and task_view["display_title"]=="统一后的中文标题",(status,task_view)
with Session(engine) as db:
    assert get_task_titles(db,managed["id"])=={
       "source_title":"源视频标题","translated_title":"统一后的中文标题"}

# Simulate divergent duplicate legacy title rows inside an automatically
# rolled back transaction, without disturbing any real task title.
title_sql=(Path(__file__).resolve().parents[1]/"migrations"/
           "20261123000021_unify_task_titles.sql").read_text()
shadow_title="""CREATE TABLE vr2_task_titles(
 task_id UUID PRIMARY KEY REFERENCES tasks(id),source_title TEXT NOT NULL,
 translated_title TEXT,updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
try:
    with engine.begin() as conn:
        conn.execute(text(shadow_title))
        conn.execute(text("""INSERT INTO vr2_task_titles(task_id,source_title)
            VALUES(CAST(:id AS uuid),'冲突的历史标题')"""),{"id":managed["id"]})
        conn.execute(text(title_sql))
    raise AssertionError("divergent title migration should be rejected")
except Exception as e:
    if isinstance(e,AssertionError):raise
    assert "conflicting task titles" in str(e),str(e)
assert fetch_one("SELECT to_regclass('public.vr2_task_titles')") is None
with engine.begin() as conn:
    conn.execute(text(shadow_title))
    conn.execute(text("""INSERT INTO vr2_task_titles(task_id,source_title,translated_title)
        VALUES(CAST(:id AS uuid),'源视频标题','统一后的中文标题')"""),{"id":managed["id"]})
    conn.execute(text(title_sql))
assert fetch_one("SELECT to_regclass('public.vr2_task_titles')") is None
with Session(engine) as db:
    assert get_task_titles(db,managed["id"])["translated_title"]=="统一后的中文标题"
print("PASS: Python/Rust share task.title.*; conflicting duplicate titles abort without loss, matching titles merge")

# Rust-owned pipeline runs now live physically in Python's pipeline_runs.
assert fetch_one("SELECT relkind FROM pg_class WHERE oid='public.vr2_workflows'::regclass")=='v'
assert fetch_one("SELECT relkind FROM pg_class WHERE oid='public.pipeline_runs'::regclass")=='r'
from videoroll.db.models import PipelineRun
old_task=str(uuid.uuid4());old_run=str(uuid.uuid4());native_run=str(uuid.uuid4())
with engine.begin() as conn:
    conn.execute(text("""INSERT INTO tasks(id,source_type,source_license,status)
        VALUES(CAST(:id AS uuid),'local','own','created')"""),{"id":old_task})
    conn.execute(text("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,request_json)
        VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'hatchet','youtube_ingest','running',
        '{"legacy_config":"untouched"}'::jsonb)"""),{"id":old_run,"task":old_task})
    conn.execute(text("""INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration)
        VALUES(CAST(:id AS uuid),CAST(:task AS uuid),1,'running','download',
        '{"rust_config":"original ledger"}'::jsonb)"""),{"id":native_run,"task":managed["id"]})
with Session(engine) as db:
    old=db.get(PipelineRun,uuid.UUID(old_run))
    new=db.get(PipelineRun,uuid.UUID(native_run))
    assert old is not None and old.engine=='hatchet' and old.request_json=={'legacy_config':'untouched'}
    assert new is not None and new.engine=='rust-v2' and new.workflow_name=='videoroll.rust.pipeline'
    assert new.request_json=={'rust_config':'original ledger'} and new.state=='running'
assert fetch_one("SELECT count(*) FROM vr2_workflows WHERE id=CAST(:id AS uuid)",id=old_run)==0
assert fetch_one("SELECT count(*) FROM vr2_workflows WHERE id=CAST(:id AS uuid)",id=native_run)==1
status,workflow=request('GET',f'/api/v2/workflows/{native_run}')
assert status==200 and workflow['id']==native_run and workflow['configuration']=={'rust_config':'original ledger'},(status,workflow)
with Session(engine) as db:
    row=db.get(PipelineRun,uuid.UUID(native_run))
    row.request_json={'rust_config':'python updated'}
    db.commit()
status,updated=request('GET',f'/api/v2/workflows/{native_run}')
assert status==200 and updated['configuration']=={'rust_config':'python updated'},(status,updated)
with engine.begin() as conn:
    conn.execute(text("""UPDATE vr2_workflows SET state='paused',current_stage='subtitle'
        WHERE id=CAST(:id AS uuid)"""),{'id':native_run})
with Session(engine) as db:
    row=db.get(PipelineRun,uuid.UUID(native_run))
    assert row.state=='paused'
    assert fetch_one("SELECT current_stage FROM pipeline_runs WHERE id=CAST(:id AS uuid)",id=native_run)=='subtitle'
try:
    with engine.begin() as conn:
        conn.execute(text("""UPDATE vr2_workflows SET engine='hatchet' WHERE id=CAST(:id AS uuid)"""),{'id':native_run})
    raise AssertionError('view allowed a Rust run to change into a Python run')
except Exception as e:
    if isinstance(e,AssertionError):raise
    assert 'check option' in str(e).lower(),str(e)
with Session(engine) as db:
    assert db.get(PipelineRun,uuid.UUID(old_run)).engine=='hatchet'
    assert db.get(PipelineRun,uuid.UUID(native_run)).engine=='rust-v2'
assert fetch_one("""SELECT count(*) FROM pg_constraint WHERE contype='f'
   AND confrelid='public.pipeline_runs'::regclass""")>=6
print("PASS: single physical pipeline_runs contains preserved Hatchet and Rust records; updatable view is scoped and fenced")

# Original operation_inbox is the sole physical job ledger. Native Stage Jobs
# carry runtime_engine=rust-v2 and are scoped out of Python legacy inbox data.
from videoroll.db.models import OperationInbox
assert fetch_one("SELECT relkind FROM pg_class WHERE oid='public.vr2_jobs'::regclass")=='v'
assert fetch_one("SELECT relkind FROM pg_class WHERE oid='public.operation_inbox'::regclass")=='r'
python_op=str(uuid.uuid4());native_op=str(uuid.uuid4());python_key='python-old-inbox-'+uuid.uuid4().hex
native_key='rust-view-inbox-'+uuid.uuid4().hex
with engine.begin() as conn:
    conn.execute(text("""INSERT INTO operation_inbox(id,operation_key,status,request_json)
      VALUES(CAST(:id AS uuid),:key,'processing','{"from":"python"}'::jsonb)"""),
      {'id':python_op,'key':python_key})
    conn.execute(text("""INSERT INTO vr2_jobs(
       id,workflow_id,task_id,stage,generation,operation_key,state,payload,attempts,max_attempts,
       lease_until,available_at)
       VALUES(CAST(:id AS uuid),CAST(:flow AS uuid),CAST(:task AS uuid),'subtitle',1,
       :key,'awaiting','{"from":"rust"}'::jsonb,2,4,now()+interval '3 minutes',now())"""),
       {'id':native_op,'flow':native_run,'task':managed['id'],'key':native_key})
with Session(engine) as db:
    old=db.get(OperationInbox,uuid.UUID(python_op))
    new=db.get(OperationInbox,uuid.UUID(native_op))
    assert old.status=='processing' and old.request_json=={'from':'python'}
    assert new.status=='awaiting' and new.request_json=={'from':'rust'}
    assert new.attempt_count==2 and new.lease_until is not None
assert fetch_one("SELECT runtime_engine FROM operation_inbox WHERE id=CAST(:id AS uuid)",id=python_op)=='python'
assert fetch_one("SELECT runtime_engine FROM operation_inbox WHERE id=CAST(:id AS uuid)",id=native_op)=='rust-v2'
assert fetch_one("SELECT count(*) FROM vr2_jobs WHERE id=CAST(:id AS uuid)",id=python_op)==0
status,stages=request('GET',f'/api/v2/workflows/{native_run}/jobs')
assert status==200 and any(j['id']==native_op and j['state']=='awaiting' for j in stages),(status,stages)
with Session(engine) as db:
    row=db.get(OperationInbox,uuid.UUID(native_op))
    row.request_json={'from':'python updated rust stage'}
    row.status='unknown';row.attempt_count=3
    db.commit()
status,stages=request('GET',f'/api/v2/workflows/{native_run}/jobs')
assert status==200 and any(j['id']==native_op and j['state']=='unknown' and j['attempts']==3 for j in stages),(status,stages)
with engine.begin() as conn:
    conn.execute(text("UPDATE vr2_jobs SET state='awaiting',attempts=4 WHERE id=CAST(:id AS uuid)"),{'id':native_op})
with Session(engine) as db:
    row=db.get(OperationInbox,uuid.UUID(native_op))
    assert row.status=='awaiting' and row.attempt_count==4
try:
    with engine.begin() as conn:
        conn.execute(text("UPDATE vr2_jobs SET runtime_engine='python' WHERE id=CAST(:id AS uuid)"),{'id':native_op})
    raise AssertionError('Rust Stage Job escaped runtime_engine fence')
except Exception as e:
    if isinstance(e,AssertionError):raise
    assert 'check option' in str(e).lower(),str(e)
# The original operation key is the global idempotency fence. It must
# reject duplicate external side effects, even across runtime engines.
try:
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO operation_inbox(id,operation_key,status)
            VALUES(CAST(:id AS uuid),:key,'pending')"""),
            {'id':str(uuid.uuid4()),'key':native_key})
    raise AssertionError('duplicate operation key was not rejected')
except Exception as e:
    if isinstance(e,AssertionError):raise
    assert 'unique' in str(e).lower(),str(e)
assert fetch_one("SELECT count(*) FROM operation_inbox WHERE operation_key=:key",key=native_key)==1
assert fetch_one("SELECT count(*) FROM operation_inbox WHERE operation_key=:key",key=python_key)==1
assert fetch_one("""SELECT count(*) FROM pg_constraint WHERE contype='f'
    AND confrelid='public.operation_inbox'::regclass""")>=5
print("PASS: single physical operation_inbox retains Python rows; Rust Stage Jobs fenced, visible, idempotent and writable")
