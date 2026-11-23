#!/usr/bin/env python3
"""Backfill real *preexisting* V2 rows into original Python tables.

Runs exclusively against the disposable PostgreSQL started by smoke-isolated.
It deliberately seeds Rust-owned jobs and real FK references *before* applying
the unification migrations, then verifies historic identity, leases, unknown
publish outcomes, Python records, view behavior and transaction-safe conflicts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

ROOT=Path(__file__).resolve().parents[1]
MIG=ROOT/"migrations"
url=os.environ["DATABASE_URL"]
assert url.endswith("/videoroll_test") and "127.0.0.1:" in url, "refusing non-isolated DB"
files=sorted(MIG.glob("*.sql"))
assert len(files)>=23
def row(db,sql,args=()):
    return db.execute(sql,args).fetchone()
def val(db,sql,args=()):
    result=row(db,sql,args)
    assert result is not None,(sql,args)
    return next(iter(result.values()))
def apply(db,path):
    with db.transaction():
        db.execute(path.read_text())

with psycopg.connect(url,autocommit=True,row_factory=dict_row) as db:
    for p in files:
        if p.name>="20261123000019":break
        apply(db,p)

    task=uuid4()
    python_task=uuid4()
    python_flow=uuid4()
    python_inbox=uuid4()
    unknown_flow=uuid4()
    unknown_job=uuid4()
    running_flow=uuid4()
    running_job=uuid4()
    fence=uuid4()
    event=uuid4()
    render_exec=uuid4()
    workdir="v2/artifacts/schema-backfill/"+str(task)
    original_key="py:historical:"+str(python_inbox)
    unknown_key="vr2:historical:publish:"+str(unknown_job)
    running_key="vr2:historical:render:"+str(running_job)
    with db.transaction():
        db.execute("""INSERT INTO tasks(id,source_type,source_license,status)
          VALUES(%s,'local','own','created'),(%s,'local','own','created')""",(task,python_task))
        db.execute("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,request_json)
          VALUES(%s,%s,'hatchet','video_workflow','running',
          '{"python_business":"preserve"}'::jsonb)""",(python_flow,python_task))
        db.execute("""INSERT INTO operation_inbox(id,operation_key,status,request_json)
          VALUES(%s,%s,'processing','{"python_inbox":"preserve"}'::jsonb)""",
          (python_inbox,original_key))
        db.execute("INSERT INTO vr2_managed_tasks(task_id,origin) VALUES(%s,'local_upload')",(task,))
        db.execute("""INSERT INTO vr2_workflows(
           id,task_id,generation,state,current_stage,configuration,last_error)
           VALUES(%s,%s,7,'unknown','publish','{"outcome":"unconfirmed"}'::jsonb,
           'do not replay unknown publish'),(%s,%s,8,'running','render',
           '{"gpu":"synthetic"}'::jsonb,NULL)""",
           (unknown_flow,task,running_flow,task))
        db.execute("""INSERT INTO vr2_jobs(
            id,workflow_id,task_id,stage,generation,operation_key,state,
            payload,result,attempts,max_attempts,external_ticket,last_error)
            VALUES(%s,%s,%s,'publish',7,%s,'unknown',
            '{"meta":"original"}'::jsonb,'{"platform":"uncertain"}'::jsonb,
            1,1,'platform-maybe-accepted','must not automatically submit')""",
            (unknown_job,unknown_flow,task,unknown_key))
        db.execute("""INSERT INTO vr2_jobs(
            id,workflow_id,task_id,stage,generation,operation_key,state,
            attempts,max_attempts,lease_token,lease_until,available_at,external_ticket)
            VALUES(%s,%s,%s,'render',8,%s,'running',
            2,4,%s,now()+interval '30 minutes',now()-interval '1 minute','worker-active')""",
            (running_job,running_flow,task,running_key,fence))
        db.execute("""INSERT INTO vr2_events(id,workflow_id,job_id,kind,details)
            VALUES(%s,%s,%s,'job.uncertain','{"external":"maybe"}'::jsonb)""",
            (event,unknown_flow,unknown_job))
        db.execute("""INSERT INTO vr2_workdirs(path_key,task_id,workflow_id,state,due_at)
            VALUES(%s,%s,%s,'retained',now()+interval '30 days')""",
            (workdir,task,running_flow))
        db.execute("""INSERT INTO vr2_render_executions(
            id,job_id,task_id,workflow_id,state,fence_token,lease_until)
            VALUES(%s,%s,%s,%s,'running',%s,now()+interval '30 minutes')""",
            (render_exec,running_job,task,running_flow,str(fence)))
        db.execute("""INSERT INTO app_settings(key,value_json,version)
            VALUES(%s,'{"source_title":"Saved source","user_field":"preserve"}'::jsonb,3)""",
            ("task.title."+str(task),))
        db.execute("""INSERT INTO vr2_task_titles(task_id,source_title,translated_title)
            VALUES(%s,'Saved source','旧任务中文字幕')""",(task,))
        db.execute("""INSERT INTO vr2_settings(key,value_json,version)
            VALUES('schema.unification.backfill','{"old":"rust"}'::jsonb,2)""")
    before_lease=val(db,"SELECT lease_until FROM vr2_jobs WHERE id=%s",(running_job,))
    before_created=val(db,"SELECT created_at FROM vr2_jobs WHERE id=%s",(running_job,))
    before_flow_created=val(db,"SELECT created_at FROM vr2_workflows WHERE id=%s",(unknown_flow,))
    for p in files:
        if p.name<"20261123000019" or p.name>="20261123000022":continue
        apply(db,p)

    assert val(db,"SELECT to_regclass('public.vr2_task_titles')") is None
    assert val(db,"SELECT runtime_owner FROM tasks WHERE id=%s",(task,))=="rust"
    assert val(db,"SELECT value_json->>'user_field' FROM app_settings WHERE key=%s",
               ("task.title."+str(task),))=="preserve"
    assert val(db,"SELECT value_json->>'translated_title' FROM app_settings WHERE key=%s",
               ("task.title."+str(task),))=="旧任务中文字幕"
    print("PASS migration 19–21: original settings/title, preserved unknown fields, Rust task ownership")

    wf_sql=(MIG/"20261123000022_unify_pipeline_runs.sql").read_text()
    try:
        with db.transaction():
            db.execute("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state)
                 VALUES(%s,%s,'hatchet','collision_fixture','running')""",
                 (unknown_flow,python_task))
            db.execute(wf_sql)
        raise AssertionError("workflow UUID collision migration must fail")
    except Exception as e:
        if isinstance(e,AssertionError):raise
        assert "workflow UUID collision" in str(e),str(e)
    assert val(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_workflows'::regclass")=="r"
    assert val(db,"SELECT count(*) FROM pipeline_runs WHERE id=%s",(unknown_flow,))==0
    apply(db,MIG/"20261123000022_unify_pipeline_runs.sql")
    assert val(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_workflows'::regclass")=="v"
    assert val(db,"SELECT count(*) FROM pipeline_runs WHERE id=%s",(unknown_flow,))==1
    assert val(db,"SELECT created_at FROM pipeline_runs WHERE id=%s",(unknown_flow,))==before_flow_created
    assert val(db,"SELECT engine FROM pipeline_runs WHERE id=%s",(unknown_flow,))=="rust-v2"
    assert val(db,"SELECT engine FROM pipeline_runs WHERE id=%s",(python_flow,))=="hatchet"
    assert val(db,"SELECT request_json->>'outcome' FROM pipeline_runs WHERE id=%s",(unknown_flow,))=="unconfirmed"
    assert val(db,"SELECT current_stage FROM pipeline_runs WHERE id=%s",(running_flow,))=="render"
    assert val(db,"SELECT count(*) FROM vr2_workflows WHERE id=%s",(python_flow,))==0
    assert val(db,"SELECT count(*) FROM vr2_events WHERE id=%s",(event,))==1
    assert val(db,"SELECT count(*) FROM vr2_workdirs WHERE path_key=%s",(workdir,))==1
    print("PASS migration 22: real preexisting workflow IDs, generations, errors, timestamps and FKs moved to pipeline_runs; Hatchet untouched")

    inbox_sql=(MIG/"20261123000023_unify_operation_inbox.sql").read_text()
    try:
        with db.transaction():
            db.execute("""INSERT INTO operation_inbox(id,operation_key,status)
              VALUES(%s,%s,'processing')""",(uuid4(),unknown_key))
            db.execute(inbox_sql)
        raise AssertionError("operation idempotency collision migration must fail")
    except Exception as e:
        if isinstance(e,AssertionError):raise
        assert "operation idempotency key or UUID collision" in str(e),str(e)
    assert val(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_jobs'::regclass")=="r"
    assert val(db,"SELECT count(*) FROM operation_inbox WHERE operation_key=%s",(unknown_key,))==0
    apply(db,MIG/"20261123000023_unify_operation_inbox.sql")
    assert val(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_jobs'::regclass")=="v"
    assert val(db,"SELECT count(*) FROM operation_inbox WHERE id=%s",(unknown_job,))==1
    assert val(db,"SELECT count(*) FROM operation_inbox WHERE id=%s",(running_job,))==1
    assert val(db,"SELECT runtime_engine FROM operation_inbox WHERE id=%s",(python_inbox,))=="python"
    assert val(db,"SELECT runtime_engine FROM operation_inbox WHERE id=%s",(running_job,))=="rust-v2"
    assert val(db,"SELECT status FROM operation_inbox WHERE id=%s",(unknown_job,))=="unknown"
    assert val(db,"SELECT external_ticket FROM operation_inbox WHERE id=%s",(unknown_job,))=="platform-maybe-accepted"
    assert val(db,"SELECT result_json->>'platform' FROM operation_inbox WHERE id=%s",(unknown_job,))=="uncertain"
    assert val(db,"SELECT status FROM operation_inbox WHERE id=%s",(running_job,))=="running"
    assert val(db,"SELECT lease_token FROM operation_inbox WHERE id=%s",(running_job,))==fence
    assert val(db,"SELECT lease_until FROM operation_inbox WHERE id=%s",(running_job,))==before_lease
    assert val(db,"SELECT created_at FROM operation_inbox WHERE id=%s",(running_job,))==before_created
    assert val(db,"SELECT attempt_count FROM operation_inbox WHERE id=%s",(running_job,))==2
    assert val(db,"SELECT max_attempts FROM operation_inbox WHERE id=%s",(running_job,))==4
    assert val(db,"SELECT count(*) FROM vr2_jobs WHERE id=%s",(python_inbox,))==0
    assert val(db,"SELECT count(*) FROM vr2_events WHERE id=%s AND job_id=%s",
               (event,unknown_job))==1
    assert val(db,"SELECT count(*) FROM vr2_render_executions WHERE id=%s AND job_id=%s",
               (render_exec,running_job))==1
    assert val(db,"SELECT status FROM operation_inbox WHERE id=%s",(python_inbox,))=="processing"
    print("PASS migration 23: preexisting unknown publish & active render lease/fence preserved exactly, no Python inbox mutation, FKs intact")

    # Verify continued native writes and Python reads from the same table.
    with db.transaction():
        db.execute("UPDATE vr2_jobs SET state='awaiting' WHERE id=%s",(running_job,))
        db.execute("UPDATE vr2_workflows SET current_stage='cleanup' WHERE id=%s",(running_flow,))
    assert val(db,"SELECT status FROM operation_inbox WHERE id=%s",(running_job,))=="awaiting"
    assert val(db,"SELECT current_stage FROM pipeline_runs WHERE id=%s",(running_flow,))=="cleanup"
    assert val(db,"SELECT operation_key FROM operation_inbox WHERE id=%s",(unknown_job,))==unknown_key
    print("PASS backfilled workflow/job views are writable, original data shared and external uncertainty remains fenced")
