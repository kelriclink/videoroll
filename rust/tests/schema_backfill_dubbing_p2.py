#!/usr/bin/env python3
"""Preexisting YouDub and Bilibili P2 Stage evidence: forward-only backfill.

Deliberately uses an isolated PostgreSQL database, not the live coordinator.
Verifies original Python DubbingJob/PublishJob are preserved, that native
Stage job UUIDs and unknown/ambiguous P2 receipts survive, and conflicting
preexisting metadata aborts without partial writes.
"""
import os,uuid
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
root=Path(__file__).resolve().parents[1]
files=sorted((root/"migrations").glob("*.sql"))
url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test"),"isolated DB only"

def first(db,sql,args=()):
    row=db.execute(sql,args).fetchone()
    assert row is not None,(sql,args)
    return next(iter(row.values()))
def apply(db,path):
    with db.transaction():
        db.execute(path.read_text())

with psycopg.connect(url,autocommit=True,row_factory=dict_row) as db:
    for path in files:
        if path.name>="20261123000026":break
        apply(db,path)
    task,flow,original_dub,original_publish=(uuid.uuid4() for _ in range(4))
    subtitle_stage,dub_stage,pub_stage,model_job=(uuid.uuid4() for _ in range(4))
    duplicate_source="test-data/example-source.mp4"
    p2_key="test-data/example-p2.mp4"
    with db.transaction():
        db.execute("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner)
            VALUES (%s,'local','own','created','rust')""",(task,))
        db.execute("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,
             generation,state,current_stage,request_json)
             VALUES (%s,%s,'rust-v2','videoroll.rust.pipeline',1,'running','publish','{}'::jsonb)""",
             (flow,task))
        db.execute("""INSERT INTO operation_inbox(
             id,operation_key,runtime_engine,workflow_id,task_id,stage,generation,status)
             VALUES (%s,%s,'rust-v2',%s,%s,'subtitle',1,'succeeded'),
                    (%s,%s,'rust-v2',%s,%s,'dubbing',1,'succeeded'),
                    (%s,%s,'rust-v2',%s,%s,'publish',1,'succeeded')""",
             (subtitle_stage,str(subtitle_stage),flow,task,
              dub_stage,str(dub_stage),flow,task,
              pub_stage,str(pub_stage),flow,task))
        db.execute("""INSERT INTO dubbing_jobs(id,task_id,status,output_json,operation_key)
            VALUES (%s,%s,'succeeded','{"original_job":"unchanged"}'::jsonb,%s)""",
            (original_dub,task,'original:dubbing:'+str(original_dub)))
        db.execute("""INSERT INTO publish_jobs(
              id,task_id,platform,state,response_json,external_id,operation_key)
              VALUES (%s,%s,'bilibili','published',
                '{"bvid":"BV1old","aid":123456,"confirmed":true}'::jsonb,
                'BV1old',%s)""",
            (original_publish,task,'original:publish:'+str(original_publish)))
        db.execute("""INSERT INTO vr2_dubbing_runs(
           job_id,workflow_id,task_id,source_video_key,source_srt_key,
           output_video_key,model_job_id,model_status,created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'succeeded',
                   '2026-01-01 12:00:00+00'::timestamptz)""",
           (dub_stage,flow,task,duplicate_source,
            'test-data/zh.srt',p2_key,model_job))
        db.execute("""INSERT INTO vr2_bilibili_p2_appends(
           publish_job_id,task_id,dubbing_job_id,aid,output_video_key,title,
           state,attempt,uploaded_filename,uploaded_cid,last_error,
           operator_evidence,claimed_at,created_at)
           VALUES (%s,%s,%s,123456,%s,'【中配】历史视频','unknown',2,
             'uploaded-p2.mp4',987654,'edit uncertain','CID not verified',
             now()-interval '30 minutes',
             '2026-01-01 12:30:00+00'::timestamptz)""",
           (pub_stage,task,dub_stage,p2_key))
    before_original_dub=first(db,"SELECT output_json FROM dubbing_jobs WHERE id=%s",(original_dub,))
    before_original_pub=first(db,"SELECT response_json FROM publish_jobs WHERE id=%s",(original_publish,))
    before_old_dub_time=first(db,"SELECT created_at FROM vr2_dubbing_runs WHERE job_id=%s",(dub_stage,))
    before_old_p2_time=first(db,"SELECT created_at FROM vr2_bilibili_p2_appends WHERE publish_job_id=%s",(pub_stage,))
    m26=root/"migrations"/"20261123000026_unify_dubbing_runs.sql"
    # Additive columns exist before simulating an unexpected preexisting
    # record. The subsequent transactional merge must reject overwrite.
    with db.transaction():
        db.execute(m26.read_text().split("DO $merge$")[0])
    try:
        with db.transaction():
            db.execute("""UPDATE operation_inbox SET dubbing_model_status='succeeded'
              WHERE id=%s""",(dub_stage,))
            db.execute(m26.read_text())
        raise AssertionError("existing dubbing evidence was silently overwritten")
    except Exception as ex:
        if isinstance(ex,AssertionError):raise
        assert "dubbing run does not match" in str(ex),str(ex)
    assert first(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_dubbing_runs'::regclass")=="r"
    assert first(db,"SELECT dubbing_model_status FROM operation_inbox WHERE id=%s",(dub_stage,)) is None
    apply(db,m26)
    assert first(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_dubbing_runs'::regclass")=="v"
    assert first(db,"SELECT dubbing_model_status FROM operation_inbox WHERE id=%s",(dub_stage,))=="succeeded"
    assert first(db,"SELECT dubbing_output_video_key FROM operation_inbox WHERE id=%s",(dub_stage,))==p2_key
    assert first(db,"SELECT dubbing_model_job_id FROM operation_inbox WHERE id=%s",(dub_stage,))==model_job
    assert first(db,"SELECT created_at FROM vr2_dubbing_runs WHERE job_id=%s",(dub_stage,))==before_old_dub_time
    assert first(db,"SELECT model_status FROM vr2_dubbing_runs WHERE job_id=%s",(dub_stage,))=="succeeded"
    assert first(db,"SELECT output_json FROM dubbing_jobs WHERE id=%s",(original_dub,))==before_original_dub
    assert first(db,"SELECT count(*) FROM vr2_dubbing_runs WHERE job_id=%s",(original_dub,))==0
    print("PASS migration 26: immutable YouDub proof moved to original operation_inbox; Python dubbing_jobs unchanged")

    m27=root/"migrations"/"20261123000027_unify_bilibili_p2.sql"
    with db.transaction():
        db.execute(m27.read_text().split("DO $merge$")[0])
    try:
        with db.transaction():
            db.execute("UPDATE operation_inbox SET p2_state='unknown' WHERE id=%s",(pub_stage,))
            db.execute(m27.read_text())
        raise AssertionError("existing P2 append result was overwritten")
    except Exception as ex:
        if isinstance(ex,AssertionError):raise
        assert "P2 append does not belong" in str(ex),str(ex)
    assert first(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_bilibili_p2_appends'::regclass")=="r"
    assert first(db,"SELECT p2_state FROM operation_inbox WHERE id=%s",(pub_stage,)) is None
    apply(db,m27)
    assert first(db,"SELECT relkind FROM pg_class WHERE oid='public.vr2_bilibili_p2_appends'::regclass")=="v"
    assert first(db,"SELECT p2_state FROM operation_inbox WHERE id=%s",(pub_stage,))=="unknown"
    assert first(db,"SELECT p2_attempt FROM operation_inbox WHERE id=%s",(pub_stage,))==2
    assert first(db,"SELECT p2_uploaded_cid FROM operation_inbox WHERE id=%s",(pub_stage,))==987654
    assert first(db,"SELECT p2_operator_evidence FROM operation_inbox WHERE id=%s",(pub_stage,))=="CID not verified"
    assert first(db,"SELECT created_at FROM vr2_bilibili_p2_appends WHERE publish_job_id=%s",(pub_stage,))==before_old_p2_time
    assert first(db,"SELECT status FROM operation_inbox WHERE id=%s",(pub_stage,))=="succeeded"
    assert first(db,"SELECT response_json FROM publish_jobs WHERE id=%s",(original_publish,))==before_original_pub
    assert first(db,"SELECT count(*) FROM publish_jobs WHERE id=%s",(pub_stage,))==0
    assert first(db,"SELECT count(*) FROM vr2_bilibili_p2_appends WHERE publish_job_id=%s",(original_publish,))==0
    with db.transaction():
        db.execute("""UPDATE vr2_bilibili_p2_appends
              SET operator_evidence='confirmed no platform edit',
                  state='failed',attempt=3 WHERE publish_job_id=%s""",(pub_stage,))
    assert first(db,"SELECT p2_state FROM operation_inbox WHERE id=%s",(pub_stage,))=="failed"
    assert first(db,"SELECT p2_attempt FROM operation_inbox WHERE id=%s",(pub_stage,))==3
    assert first(db,"SELECT response_json FROM publish_jobs WHERE id=%s",(original_publish,))==before_original_pub
    print("PASS migration 27: P2 ambiguous/unknown side effect, claimed time, CID and retry counter persist on original stage; P1 receipt untouched")
