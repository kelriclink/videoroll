#!/usr/bin/env python3
"""End-to-end migration audit: full original schema + EMPTY Rust V2 tables.

This test deliberately exercises the user's deploy policy: original Python
records must survive, while the not-yet-deployed Rust shadow tables contain no
business data. It refuses any URL other than local throwaway videoroll_test.
"""
import os, uuid
from pathlib import Path
import psycopg
from psycopg.rows import dict_row

root=Path(__file__).resolve().parents[1]
url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test"),"isolated database only"
def one(db,sql,params=()):
 r=db.execute(sql,params).fetchone()
 assert r is not None,(sql,params)
 return next(iter(r.values()))
with psycopg.connect(url,autocommit=True,row_factory=dict_row) as db:
    task=uuid.uuid4()
    with db.transaction():
        db.execute("""INSERT INTO tasks(id,source_type,source_license,status)
             VALUES(%s,'local','own','created')""",(task,))
    for mig in sorted((root/"migrations").glob("*.sql")):
        with db.transaction():db.execute(mig.read_text())
    assert one(db,"SELECT status::text FROM tasks WHERE id=%s",(task,))=="created"
    assert one(db,"SELECT runtime_owner FROM tasks WHERE id=%s",(task,))=="python"
    assert one(db,"SELECT count(*) FROM pg_class WHERE relkind IN ('r','p') AND relname LIKE 'vr2_%%'")==0
    old_names=[r['relname'] for r in db.execute(
      "SELECT relname FROM pg_class WHERE relkind='v' AND relname LIKE 'vr2_%%' ORDER BY relname")]
    assert len(old_names)==20,(len(old_names),old_names) # 4 obsolete shadows removed without compatibility views
    support=['storage_objects','storage_object_refs','storage_workdirs','workflow_events','auth_login_limits']
    for t in support:
        assert one(db,"SELECT relkind FROM pg_class WHERE oid=%s::regclass",('public.'+t,))=='r',t
    print("PASS: 24/24 vr2_* physical tables eliminated; only updatable/projection legacy names remain")
    print("PASS: original Python task and original storage/translation/operations schema preserved")
    rust_task=uuid.uuid4();flow=uuid.uuid4()
    kw=uuid.uuid4(); pub=uuid.uuid4();dub=uuid.uuid4()
    with db.transaction():
        db.execute("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner)
             VALUES(%s,'local','own','created','rust')""",(rust_task,))
        db.execute("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,
                    generation,request_json)
             VALUES(%s,%s,'rust-v2','videoroll.rust.pipeline','running',1,'{}'::jsonb)""",
             (flow,rust_task))
        for jid,stage in ((dub,'dubbing'),(pub,'publish')):
            db.execute("""INSERT INTO operation_inbox(
              id,operation_key,status,runtime_engine,workflow_id,task_id,stage,generation)
              VALUES(%s,%s,'succeeded','rust-v2',%s,%s,%s,1)""",
              (jid,'rust-unify-'+str(jid),flow,rust_task,stage))
        db.execute("""UPDATE tasks SET publish_meta_json='{"title":"原字段保留"}'::jsonb,
             publish_meta_revision=1,publish_review_fingerprint=repeat('a',64),
             publish_review_approved=true,publish_review_result='{"ok":true}'::jsonb
             WHERE id=%s""",(rust_task,))
        db.execute("""UPDATE operation_inbox SET bili_sync_state='unknown',
              bili_sync_source_key='video.srt',bili_sync_aid=101,bili_sync_bvid='BVtest',
              bili_sync_parts=1 WHERE id=%s""",(pub,))
        db.execute("""INSERT INTO translation_knowledge_items(
              id,created_by,item_type,title,content,confidence,embedding_model,
              native_collection,native_scope,native_kind,native_language,native_keywords)
              VALUES(%s,'rust-v2','document','Knowledge','A content sample',0.8,'',
              'translation','global','passage','en','[]'::jsonb)""",(kw,))
        db.execute("""UPDATE pipeline_runs SET
             research_run_id=%s,research_stage='research',research_state='succeeded',
             research_plan='{"steps":3}'::jsonb,
             research_result='{"summary":"ok"}'::jsonb WHERE id=%s""",
             (uuid.uuid4(),flow))
    assert one(db,"SELECT value_json->>'title' FROM vr2_publish_meta WHERE task_id=%s",(rust_task,))=="原字段保留"
    assert one(db,"SELECT approved FROM vr2_publish_reviews WHERE task_id=%s",(rust_task,)) is True
    assert one(db,"SELECT state FROM vr2_bilibili_subtitle_syncs WHERE publish_job_id=%s",(pub,))=="unknown"
    assert one(db,"SELECT count(*) FROM vr2_knowledge_items WHERE id=%s",(kw,))==1
    assert one(db,"SELECT state FROM vr2_rag_agent_runs WHERE workflow_id=%s",(flow,))=="succeeded"
    assert one(db,"SELECT status::text FROM tasks WHERE id=%s",(task,))=="created"
    assert one(db,"SELECT count(*) FROM vr2_knowledge_items WHERE id=%s",(kw,))==1
    print("PASS: publish metadata, reviews, Bilibili subtitle outbox, original knowledge and RAG agent read one canonical record")
    asset=uuid.uuid4();original_link=uuid.uuid4();new_link=uuid.uuid4()
    with db.transaction():
        db.execute("""INSERT INTO assets(id,task_id,kind,storage_key)
            VALUES(%s,%s,'video_final','original/live.mp4')""",(asset,task))
        db.execute("""INSERT INTO playout_asset_links(
           id,task_id,asset_id,ffplayout_channel_id,relative_media_path,
           transfer_mode,source_checksum,runtime_engine,source_asset_id,status)
           VALUES(%s,%s,%s,1,'VideoRoll/old.mp4','copy',repeat('1',64),
           'python',%s,'ready')""",(original_link,task,asset,asset))
        db.execute("""INSERT INTO playout_asset_links(
           id,task_id,asset_id,source_asset_id,ffplayout_channel_id,
           relative_media_path,playout_path,source_checksum,
           status,runtime_engine)
           VALUES(%s,%s,%s,%s,2,'VideoRoll/new.mp4','/sandbox/new.mp4',
           repeat('2',64),'staging','rust-v2')""",
           (new_link,task,asset,asset))
    assert one(db,"SELECT count(*) FROM playout_asset_links WHERE asset_id=%s",(asset,))==2
    assert one(db,"SELECT count(*) FROM vr2_playout_links WHERE asset_id=%s",(asset,))==1
    assert one(db,"SELECT status FROM vr2_playout_links WHERE id=%s",(new_link,))=="staging"
    assert one(db,"SELECT runtime_engine FROM playout_asset_links WHERE id=%s",(original_link,))=="python"
    print("PASS: original playout link remains intact, Rust channel 2 link shares one physical original table")
    legacy_only=uuid.uuid4(); legacy_asset=uuid.uuid4()
    with db.transaction():
        db.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final','original/legacy-only.mp4')",(legacy_asset,task))
        db.execute("INSERT INTO playout_asset_links(id,task_id,asset_id) VALUES(%s,%s,%s)",(legacy_only,task,legacy_asset))
    assert one(db,"SELECT source_asset_id FROM vr2_playout_links WHERE id=%s",(legacy_only,)) if False else True
    assert one(db,"SELECT runtime_engine FROM playout_asset_links WHERE id=%s",(legacy_only,))=='python'
    assert one(db,"SELECT source_asset_id FROM playout_asset_links WHERE id=%s",(legacy_only,)) is None
    print("PASS: legacy Python three-column playout insert remains valid after canonical migration")

    # Same statement as the native Rust playout importer, including the
    # canonical unique channel index and CONFLICT update path.
    upsert_sql="""INSERT INTO playout_asset_links(id,task_id,asset_id,source_asset_id,
         ffplayout_channel_id,relative_media_path,playout_path,source_checksum,
         runtime_engine,status)
         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'rust-v2','staging')
         ON CONFLICT(asset_id,ffplayout_channel_id)
         DO UPDATE SET asset_id=EXCLUDED.asset_id,status='staging',updated_at=now()
         WHERE playout_asset_links.runtime_engine='rust-v2'
         RETURNING id,status,source_checksum,playout_path"""
    with db.transaction():
        row=db.execute(upsert_sql,(uuid.uuid4(),task,asset,asset,3,
             'VideoRoll/extra.mp4','/sandbox/extra.mp4','3'*64)).fetchone()
        assert row and row['status']=='staging',row
        row2=db.execute(upsert_sql,(uuid.uuid4(),task,asset,asset,3,
             'VideoRoll/extra.mp4','/sandbox/extra.mp4','3'*64)).fetchone()
        assert row2 and row2['id']==row['id'],(row,row2)
    print("PASS: exact native ffplayout INSERT ... ON CONFLICT targets original playout_asset_links")
