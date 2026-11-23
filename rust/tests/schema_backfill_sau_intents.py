#!/usr/bin/env python3
"""Backfill historical SAU coordinator intent while retaining original PublishJob receipts."""
import os,uuid
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
root=Path(__file__).resolve().parents[1]
uri=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in uri and uri.endswith("/videoroll_test")
files=sorted((root/"migrations").glob("*.sql"))
def one(db,query,args=()):
    r=db.execute(query,args).fetchone()
    assert r is not None,(query,args)
    return next(iter(r.values()))
def apply(db,path):
    with db.transaction():db.execute(path.read_text())
with psycopg.connect(uri,autocommit=True,row_factory=dict_row) as db:
    for path in files:
        if path.name>="20261123000028":break
        apply(db,path)
    task,receipt_id,accepted_id,unknown_id,account=(uuid.uuid4() for _ in range(5))
    with db.transaction():
        db.execute("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner)
         VALUES(%s,'local','own','created','rust')""",(task,))
        db.execute("""INSERT INTO publish_jobs
          (id,task_id,platform,account_id,state,response_json,external_id,operation_key)
          VALUES(%s,%s,'douyin',%s,'submitted',
            '{"sidecar":true,"status":"submitted"}'::jsonb,'external-video-123',%s)""",
          (receipt_id,task,account,'sidecar:real:'+str(receipt_id)))
        db.execute("""INSERT INTO vr2_social_publish_intents(
           id,task_id,platform,account_id,video_key,cover_key,metadata,
           platform_options,fingerprint,state,sau_job_id,external_id,
           external_url,attempt,dispatched_at,accepted_at,created_at)
           VALUES(%s,%s,'douyin',%s,'media/source.mp4',NULL,
           '{"title":"original metadata"}'::jsonb,'{"privacy":"public"}'::jsonb,
           'fp-confirmed','accepted',%s,NULL,NULL,1,
           now()-interval '20 minutes',now()-interval '19 minutes',
           '2026-01-01 14:00:00+00'::timestamptz)""",
           (accepted_id,task,account,receipt_id))
        db.execute("""INSERT INTO vr2_social_publish_intents(
           id,task_id,platform,account_id,video_key,cover_key,metadata,
           platform_options,fingerprint,state,last_error,attempt,
           dispatched_at,created_at)
           VALUES(%s,%s,'xiaohongshu',%s,'media/source.mp4',NULL,
           '{"title":"uncertain metadata"}'::jsonb,'{}'::jsonb,
           'fp-unknown','unknown','sidecar may have accepted this',2,
           now()-interval '40 minutes',
           '2026-01-01 15:00:00+00'::timestamptz)""",
           (unknown_id,task,account))
    before_receipt=one(db,"SELECT response_json FROM publish_jobs WHERE id=%s",(receipt_id,))
    old_created=one(db,"SELECT created_at FROM vr2_social_publish_intents WHERE id=%s",(unknown_id,))
    migration=root/"migrations"/"20261123000028_unify_social_publish_intents.sql"
    try:
        with db.transaction():
            db.execute("""INSERT INTO operation_inbox(id,operation_key,status)
                VALUES(%s,%s,'processing')""",
                (uuid.uuid4(),'v2:sau:intent:'+str(accepted_id)))
            db.execute(migration.read_text())
        raise AssertionError("conflicting original operation key was silently reused")
    except Exception as ex:
        if isinstance(ex,AssertionError):raise
        assert "SAU intent UUID or idempotency key conflicts" in str(ex),str(ex)
    assert one(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_social_publish_intents'::regclass")=="r"
    assert one(db,"SELECT count(*) FROM operation_inbox WHERE id=%s",(accepted_id,))==0
    apply(db,migration)
    assert one(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_social_publish_intents'::regclass")=="v"
    assert one(db,"SELECT status FROM operation_inbox WHERE id=%s",(accepted_id,))=="accepted"
    assert one(db,"SELECT stage FROM operation_inbox WHERE id=%s",(accepted_id,))=="sau_publish"
    assert one(db,"SELECT workflow_id FROM operation_inbox WHERE id=%s",(accepted_id,)) is None
    assert one(db,"SELECT runtime_engine FROM operation_inbox WHERE id=%s",(accepted_id,))=="rust-v2"
    assert one(db,"SELECT sau_job_id FROM operation_inbox WHERE id=%s",(accepted_id,))==receipt_id
    assert one(db,"SELECT sau_account_id FROM operation_inbox WHERE id=%s",(accepted_id,))==account
    assert one(db,"SELECT sau_platform_options->>'privacy' FROM operation_inbox WHERE id=%s",(accepted_id,))=="public"
    assert one(db,"SELECT request_json->>'title' FROM operation_inbox WHERE id=%s",(accepted_id,))=="original metadata"
    assert one(db,"SELECT created_at FROM operation_inbox WHERE id=%s",(unknown_id,))==old_created
    assert one(db,"SELECT status FROM operation_inbox WHERE id=%s",(unknown_id,))=="unknown"
    assert one(db,"SELECT attempt_count FROM operation_inbox WHERE id=%s",(unknown_id,))==2
    assert one(db,"SELECT max_attempts FROM operation_inbox WHERE id=%s",(unknown_id,))==1
    assert one(db,"SELECT last_error FROM operation_inbox WHERE id=%s",(unknown_id,))=="sidecar may have accepted this"
    assert one(db,"SELECT count(*) FROM publish_jobs WHERE task_id=%s",(task,))==1
    assert one(db,"SELECT response_json FROM publish_jobs WHERE id=%s",(receipt_id,))==before_receipt
    assert one(db,"SELECT sau_job_id FROM vr2_social_publish_intents WHERE id=%s",(accepted_id,))==receipt_id
    assert one(db,"SELECT count(*) FROM vr2_jobs WHERE stage='sau_publish' AND workflow_id IS NULL")==2
    assert one(db,"""SELECT count(*) FROM vr2_jobs j
       JOIN vr2_workflows w ON w.id=j.workflow_id
       WHERE j.stage='sau_publish'""")==0
    print("PASS migration 28: SAU accepted/unknown sidecar intents migrated to original operation_inbox; external publish_jobs receipt unchanged")

    with db.transaction():
        db.execute("""UPDATE vr2_social_publish_intents
            SET state='submitted',external_id='external-video-123',
              external_url='https://example.invalid/video-123'
            WHERE id=%s AND state='accepted'""",(accepted_id,))
    assert one(db,"SELECT status FROM operation_inbox WHERE id=%s",(accepted_id,))=="submitted"
    assert one(db,"SELECT sau_external_id FROM operation_inbox WHERE id=%s",(accepted_id,))=="external-video-123"
    assert one(db,"SELECT status FROM operation_inbox WHERE id=%s",(unknown_id,))=="unknown"
    assert one(db,"SELECT count(*) FROM publish_jobs WHERE task_id=%s",(task,))==1
    try:
        with db.transaction():
            db.execute("""INSERT INTO operation_inbox(id,operation_key,status,runtime_engine,
                task_id,stage,generation,sau_platform,sau_account_id,sau_video_key,
                sau_fingerprint,attempt_count,max_attempts)
                VALUES(%s,%s,'queued','rust-v2',%s,'sau_publish',0,
                'douyin',%s,'media/another.mp4','another-fp',1,1)""",
                (uuid.uuid4(),'v2:sau:intent:'+str(uuid.uuid4()),task,account))
        raise AssertionError("duplicated platform/account intent was admitted")
    except Exception as ex:
        if isinstance(ex,AssertionError):raise
        assert "unique" in str(ex).lower(),str(ex)
    assert one(db,"SELECT status FROM operation_inbox WHERE id=%s",(unknown_id,))=="unknown"
    print("PASS SAU original-table state/receipt reconciliation, unknown/no-replay and per-target uniqueness")
