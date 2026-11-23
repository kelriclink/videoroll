#!/usr/bin/env python3
"""Validate preexisting Python and native Rust worker data through migrations 24/25.

Runs only on the throwaway PostgreSQL fixture. Seeds both wire protocols,
legacy & native credentials, queued and running executions, and verifies
lossless FK/lease/fencing migration plus collision rollback.
"""
import hashlib,os
from pathlib import Path
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row

ROOT=Path(__file__).resolve().parents[1]
MIG=ROOT/"migrations"
url=os.environ["DATABASE_URL"]
assert url.endswith("/videoroll_test") and "127.0.0.1:" in url, "test database only"

def one(db,sql,values=()):
    found=db.execute(sql,values).fetchone()
    assert found is not None,(sql,values)
    return next(iter(found.values()))
def apply(db,file):
    with db.transaction():db.execute(file.read_text())
def hash_of(value):
    return hashlib.sha256(value.encode()).hexdigest()

with psycopg.connect(url,autocommit=True,row_factory=dict_row) as db:
    for file in sorted(MIG.glob("*.sql")):
        if file.name>="20261123000024":break
        apply(db,file)

    task=uuid4();workflow=uuid4()
    job=uuid4();queued_job=uuid4()
    native_worker=uuid4();old_worker=uuid4()
    native_enroll=uuid4();old_enroll=uuid4()
    native_running=uuid4();native_queued=uuid4()
    old_exec=uuid4();old_render_job=uuid4()
    fence="native-fence-"+str(uuid4())
    old_fence="legacy-fence-"+str(uuid4())
    native_credential="9"*64
    old_credential="vrw_"+str(uuid4())+"-auth-key"
    native_invitation="7"*64
    old_invitation="vre_"+str(uuid4())
    with db.transaction():
        db.execute("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,runtime_origin)
          VALUES(%s,'local','own','created','rust','local_upload')""",(task,))
        db.execute("""INSERT INTO pipeline_runs(
          id,task_id,engine,workflow_name,generation,current_stage,state,request_json)
          VALUES(%s,%s,'rust-v2','videoroll.rust.pipeline',5,'render','running',
          '{"quality":"full"}'::jsonb)""",(workflow,task))
        for jid,stage,key in ((job,'render','rust:preexisting-render-'+str(job)),
                              (queued_job,'publish','rust:preexisting-queued-'+str(queued_job))):
            db.execute("""INSERT INTO operation_inbox(
               id,operation_key,status,runtime_engine,task_id,workflow_id,stage,generation)
               VALUES(%s,%s,'awaiting','rust-v2',%s,%s,%s,5)""",
               (jid,key,task,workflow,stage))
        db.execute("""INSERT INTO render_workers(id,worker_key,name,platform,
          credential_hash,credential_issued_at,status,enabled,max_concurrency)
          VALUES(%s,'existing-python-worker','Original Linux Worker','linux',%s,
          now(),'online',true,2)""",(old_worker,hash_of(old_credential)))
        db.execute("""INSERT INTO render_worker_enrollments(
          id,token_hash,label,status,expires_at,worker_id)
          VALUES(%s,%s,'original invitation','consumed',now()+interval '1 hour',%s)""",
          (old_enroll,hash_of(old_invitation),old_worker))
        db.execute("""INSERT INTO render_jobs(
          id,task_id,status,request_json,lease_owner,lease_until)
          VALUES(%s,%s,'running','{"original":"job"}'::jsonb,('render-worker:'||%s),
          now()+interval '30 minutes')""",(old_render_job,task,str(old_exec)))
        db.execute("""INSERT INTO render_executions(
          id,render_job_id,worker_id,attempt,fence_token,state,
          progress,render_spec,metrics,output_json,lease_until,log_tail)
          VALUES(%s,%s,%s,1,%s,'running',12,'{"original":1}'::jsonb,
          '{"legacy":1}'::jsonb,'{"original_output":"retained"}'::jsonb,
          now()+interval '30 minutes','original worker log')""",
          (old_exec,old_render_job,old_worker,old_fence))
        db.execute("""INSERT INTO vr2_render_workers(
           id,worker_key,name,platform,credential_hash,status,enabled,
           max_concurrency,capabilities,labels)
           VALUES(%s,'native-rust-worker','Original Native Worker','linux',%s,
           'busy',true,3,'{"h264":true}'::jsonb,'{"gpu":"A380"}'::jsonb)""",
           (native_worker,hash_of(native_credential)))
        db.execute("""INSERT INTO vr2_render_enrollments(
           id,token_hash,label,expires_at,consumed_at,worker_id)
           VALUES(%s,%s,'native invitation',now()+interval '1 hour',now(),%s)""",
           (native_enroll,hash_of(native_invitation),native_worker))
        db.execute("""INSERT INTO vr2_render_executions(
           id,job_id,task_id,workflow_id,worker_id,attempt,fence_token,
           state,mode,render_spec,progress,lease_until,metrics,log_tail)
           VALUES(%s,%s,%s,%s,%s,2,%s,'running','burn_in',
           '{"schema_version":1}'::jsonb,37,now()+interval '30 minutes',
           '{"fps":30}'::jsonb,'native worker log')""",
           (native_running,job,task,workflow,native_worker,fence))
        db.execute("""INSERT INTO vr2_render_executions(
           id,job_id,task_id,workflow_id,worker_id,attempt,fence_token,state,
           mode,render_spec,progress,metrics)
           VALUES(%s,%s,%s,%s,NULL,0,NULL,'queued','burn_in',
           '{}'::jsonb,0,'{}'::jsonb)""",(native_queued,queued_job,task,workflow))

    old_created=one(db,"SELECT created_at FROM render_executions WHERE id=%s",(old_exec,))
    running_lease=one(db,"SELECT lease_until FROM vr2_render_executions WHERE id=%s",(native_running,))
    native_created=one(db,"SELECT created_at FROM vr2_render_executions WHERE id=%s",(native_running,))

    migration24=MIG/"20261123000024_unify_render_workers.sql"
    try:
        with db.transaction():
            db.execute("""INSERT INTO render_workers(
              id,worker_key,name,platform) VALUES(
              %s,'native-rust-worker','colliding python row','linux')""",(uuid4(),))
            db.execute(migration24.read_text())
        raise AssertionError("duplicate worker_key must abort migration")
    except Exception as error:
        if isinstance(error,AssertionError):raise
        assert "identity collision" in str(error),str(error)
    assert one(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_render_workers'::regclass")=="r"
    assert one(db,"SELECT count(*) FROM render_workers WHERE id=%s",(native_worker,))==0

    apply(db,migration24)
    assert one(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_render_workers'::regclass")=="v"
    assert one(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_render_enrollments'::regclass")=="v"
    assert one(db,"SELECT runtime_engine FROM render_workers WHERE id=%s",(old_worker,))=="python"
    assert one(db,"SELECT runtime_engine FROM render_workers WHERE id=%s",(native_worker,))=="rust-v2"
    assert one(db,"SELECT credential_hash FROM render_workers WHERE id=%s",(old_worker,))==hash_of(old_credential)
    assert one(db,"SELECT credential_hash FROM render_workers WHERE id=%s",(native_worker,))==hash_of(native_credential)
    assert one(db,"SELECT runtime_engine FROM render_worker_enrollments WHERE id=%s",(old_enroll,))=="python"
    assert one(db,"SELECT runtime_engine FROM render_worker_enrollments WHERE id=%s",(native_enroll,))=="rust-v2"
    assert one(db,"SELECT status FROM render_worker_enrollments WHERE id=%s",(native_enroll,))=="consumed"
    assert one(db,"SELECT count(*) FROM vr2_render_workers WHERE id=%s",(old_worker,))==0
    assert one(db,"SELECT count(*) FROM vr2_render_enrollments WHERE id=%s",(old_enroll,))==0
    assert one(db,"SELECT max_concurrency FROM render_workers WHERE id=%s",(native_worker,))==3
    assert one(db,"SELECT count(*) FROM vr2_render_executions WHERE id=%s AND worker_id=%s",
               (native_running,native_worker))==1
    print("PASS migration 24: original/native worker credentials and enrollments stored once, full identity & capacity preserved, collisions roll back")

    migration25=MIG/"20261123000025_unify_render_executions.sql"
    try:
        with db.transaction():
            db.execute("""INSERT INTO render_executions(
              id,render_job_id,worker_id,attempt,fence_token)
              VALUES(%s,%s,%s,2,%s)""",
              (native_running,old_render_job,old_worker,"colliding-fence-"+str(uuid4())))
            db.execute(migration25.read_text())
        raise AssertionError("duplicate execution UUID must abort migration")
    except Exception as error:
        if isinstance(error,AssertionError):raise
        assert "render execution UUID collision" in str(error),str(error)
    assert one(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_render_executions'::regclass")=="r"
    assert one(db,"SELECT count(*) FROM render_executions WHERE id=%s",(native_running,))==0
    apply(db,migration25)
    assert one(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_render_executions'::regclass")=="v"
    assert one(db,"SELECT runtime_engine FROM render_executions WHERE id=%s",(old_exec,))=="python"
    assert one(db,"SELECT runtime_engine FROM render_executions WHERE id=%s",(native_running,))=="rust-v2"
    assert one(db,"SELECT created_at FROM render_executions WHERE id=%s",(old_exec,))==old_created
    assert one(db,"SELECT created_at FROM render_executions WHERE id=%s",(native_running,))==native_created
    assert one(db,"SELECT lease_until FROM render_executions WHERE id=%s",(native_running,))==running_lease
    assert one(db,"SELECT fence_token FROM render_executions WHERE id=%s",(native_running,))==fence
    assert one(db,"SELECT render_job_id FROM render_executions WHERE id=%s",(old_exec,))==old_render_job
    assert one(db,"SELECT job_id FROM render_executions WHERE id=%s",(native_running,))==job
    assert one(db,"SELECT attempt FROM render_executions WHERE id=%s",(native_running,))==2
    assert one(db,"SELECT progress FROM render_executions WHERE id=%s",(native_running,))==37
    assert one(db,"SELECT worker_id FROM render_executions WHERE id=%s",(native_queued,)) is None
    assert one(db,"SELECT fence_token FROM render_executions WHERE id=%s",(native_queued,)) is None
    assert one(db,"SELECT attempt FROM render_executions WHERE id=%s",(native_queued,))==0
    assert one(db,"SELECT count(*) FROM vr2_render_executions WHERE id=%s",(old_exec,))==0
    assert one(db,"SELECT count(*) FROM vr2_render_executions WHERE id=%s",(native_running,))==1
    assert one(db,"SELECT log_tail FROM render_executions WHERE id=%s",(old_exec,))=="original worker log"
    assert one(db,"SELECT log_tail FROM render_executions WHERE id=%s",(native_running,))=="native worker log"
    assert one(db,"SELECT output_json->>'original_output' FROM render_executions WHERE id=%s",(old_exec,))=="retained"
    with db.transaction():
        db.execute("UPDATE vr2_render_executions SET progress=45,state='uploading' WHERE id=%s",(native_running,))
    assert one(db,"SELECT progress FROM render_executions WHERE id=%s",(native_running,))==45
    assert one(db,"SELECT state FROM render_executions WHERE id=%s",(native_running,))=="uploading"
    try:
        with db.transaction():
            db.execute("UPDATE vr2_render_executions SET runtime_engine='python' WHERE id=%s",(native_running,))
        raise AssertionError("native execution cannot change engine via view")
    except Exception as error:
        if isinstance(error,AssertionError):raise
        assert "check option" in str(error).lower() or "ck_render_exec_runtime_links" in str(error).lower(),str(error)
    assert one(db,"SELECT runtime_engine FROM render_executions WHERE id=%s",(native_running,))=="rust-v2"
    print("PASS migration 25: original/native execution history, queued-null worker, running fence/lease/attempt, durable output and view safety preserved")
