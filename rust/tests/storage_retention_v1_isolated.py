"""Exercise terminal retention scheduling using the actual Rust GC and disposable PG."""
import json
import os
import subprocess
import shutil
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import psycopg

uri=os.environ["DATABASE_URL"]
parsed=urlsplit(uri)
assert parsed.hostname=="127.0.0.1" and parsed.path=="/videoroll_test"
assert os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]=="v2-local-test-only-0123456789abcdef"
def db():
    return psycopg.connect(uri)
def seed(status,age,stopped=None):
    task,flow=uuid4(),uuid4()
    key=f"v2/raw/{task}/fixture.mp4"
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,stopped_status,runtime_owner,updated_at) VALUES(%s,'local','own',%s,%s,'rust',now()-(%s*interval '1 hour'))",(task,status,stopped,age))
        c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES(%s,%s,1,%s,'download')",(flow,task,"succeeded" if status=="published" else status))
        c.execute("INSERT INTO storage_objects(storage_key,size_bytes,retain_until,state) VALUES(%s,4,now()+interval '300 days','live')",(key,))
        c.execute("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(%s,'task',%s,'video_raw')",(key,task))
    return task,flow,key
def gc(hours=None):
    env=os.environ.copy()
    env.pop("FAILED_TASK_RESOURCE_RETENTION_HOURS",None)
    if hours is not None: env["FAILED_TASK_RESOURCE_RETENTION_HOURS"]=str(hours)
    env["VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP"]="false"
    subprocess.run([os.environ.get("VIDEOROLL_TEST_BIN","./target/debug/videoroll-v2"),"gc","--limit","50"],env=env,check=True)
def ttl(days):
    with db() as c:
        c.execute("INSERT INTO app_settings(key,value_json) VALUES('storage.retention',%s::jsonb) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({"asset_ttl_days":days}),))
def check(item,scheduled):
    task,flow,key=item
    with db() as c:
        refs=c.execute("SELECT count(*) FROM storage_object_refs WHERE storage_key=%s",(key,)).fetchone()[0]
        events=c.execute("SELECT count(*) FROM workflow_events WHERE workflow_id=%s AND kind='storage.cleanup_scheduled'",(flow,)).fetchone()[0]
        dirs=c.execute("SELECT count(*) FROM storage_workdirs WHERE task_id=%s",(task,)).fetchone()[0]
        assert (refs,events,dirs)==((0,1,4) if scheduled else (1,0,0)),(item,scheduled,refs,events,dirs)
        if scheduled:
            assert c.execute("SELECT retain_until>now()+interval '50 minutes' FROM storage_objects WHERE storage_key=%s",(key,)).fetchone()[0]
old_failed=seed("failed",72)
recent_failed=seed("failed",24)
old_canceled=seed("canceled",72)
paused=seed("canceled",72,"rendered")
recent_canceled=seed("canceled",12)
published=seed("published",72)
ttl(0)
gc()
check(old_failed,True)
for item in (recent_failed,old_canceled,paused,recent_canceled,published):
    check(item,False)
ttl(1)
gc()
for item in (old_failed,old_canceled,published): check(item,True)
for item in (recent_failed,paused,recent_canceled): check(item,False)
gc(hours=12)
check(recent_failed,True)
check(paused,False)
check(recent_canceled,False)
gc(hours=12)
for item in (old_failed,recent_failed,old_canceled,published): check(item,True)
print("PASS: original failed 48h default and configurable override, canceled/published TTL=0 and 1d, paused preservation, single durable cleanup event")

# Force only the queued lease deadlines, then change the governing retention.
# The physical file, old asset row and orphan workdir must still be retained.
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name=="v2-smoke-objects"
bad=seed("failed",72)
held=seed("failed",72)
orphan=seed("failed",72)
gc()
fixtures=[old_failed,recent_failed,old_canceled,published,bad,held,orphan]
def force_due(items):
    with db() as c:
        for task,flow,key in items:
            c.execute("UPDATE storage_objects SET retain_until=now()-interval '1 minute' WHERE storage_key=%s",(key,))
            c.execute("UPDATE storage_workdirs SET due_at=now()-interval '1 minute' WHERE task_id=%s",(task,))
def state(item):
    with db() as c:
        task,_,key=item
        return (c.execute("SELECT state FROM storage_objects WHERE storage_key=%s",(key,)).fetchone()[0],
                c.execute("SELECT count(*) FROM assets WHERE task_id=%s AND storage_key=%s",(task,key)).fetchone()[0])
def retained(item,rows=1):
    assert (root/item[2]).exists(),item
    assert state(item)==("live",rows),(item,state(item))
def deleted(item):
    assert not (root/item[2]).exists(),item
    assert state(item)==("deleted",0),(item,state(item))
def workdir(item):
    return root/f"v2/sub/{item[0]}"
try:
    for item in fixtures:
        task,_,key=item
        p=root/key
        p.parent.mkdir(parents=True,exist_ok=True)
        if item==bad: p.mkdir()
        else: p.write_bytes(b"test")
        workdir(item).mkdir(parents=True,exist_ok=True)
        (workdir(item)/"orphan.tmp").write_text("test")
        if item!=orphan:
            with db() as c:
                c.execute("INSERT INTO assets(id,task_id,kind,storage_key,size_bytes) VALUES(%s,%s,%s,%s,4)",
                    (uuid4(),task,"video_final" if item==published else "video_raw",key))
    with db() as c:
        c.execute("UPDATE tasks SET updated_at=now()-interval '1 hour' WHERE id=%s",(old_failed[0],))
        c.execute("UPDATE storage_objects SET manual_hold_until=now()+interval '1 day' WHERE storage_key=%s",(held[2],))
    ttl(0)
    force_due(fixtures)
    gc()
    for item in (old_failed,recent_failed,old_canceled,published,bad,held): retained(item)
    for item in (old_failed,recent_failed,old_canceled,published): assert workdir(item).is_dir(),item
    deleted(orphan)
    assert not workdir(orphan).exists()
    # An unlink failure must roll back the SQL asset deletion.
    assert (root/bad[2]).is_dir()
    # Reenable TTL and shorten failed retention; eligible physical GC resumes.
    ttl(1)
    force_due(fixtures)
    gc(hours=12)
    for item in (recent_failed,old_canceled,published): deleted(item)
    for item in (recent_failed,old_canceled,published): assert not workdir(item).exists(),item
    for item in (old_failed,bad,held): retained(item)
    print("PASS: queued TTL reevaluation, changed failed retention and task age, manual hold, orphan ownership, workdir preservation, rollback on unlink failure")
finally:
    for item in fixtures:
        shutil.rmtree(root/f"v2/raw/{item[0]}",ignore_errors=True)
        shutil.rmtree(workdir(item),ignore_errors=True)

# Original-layout files produced by the Rust ASR/YouTube writers must be
# released and collected under the same policy. Unknown/shared/mutated
# legacy files must survive even if a native reference exists.
import hashlib
payload=b"registered original fixture"
digest=hashlib.sha256(payload).hexdigest()
legacy=[]
for case in ("audio","source","translated","cover","shared","wrong_digest","changed","unknown"):
    task,flow,unused=seed("published",72)
    if case=="cover": key=f"final/{task}/cover_youtube_{digest[:16]}_123456789abc.jpg"; kind="cover_image"
    elif case in ("source","translated"): key=f"sub/{task}/{'subtitle_segments' if case=='translated' else 'segments'}_{digest[:16]}_123456789abc.json"; kind="segments_json"
    elif case=="unknown": key=f"work/{task}/arbitrary.wav"; kind="audio_wav"
    else: key=f"work/{task}/audio_{digest[:16]}_123456789abc.wav"; kind="audio_wav"
    p=root/key;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(payload)
    with db() as c:
        c.execute("DELETE FROM storage_object_refs WHERE storage_key=%s",(unused,))
        c.execute("DELETE FROM storage_objects WHERE storage_key=%s",(unused,))
        c.execute("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,retain_until,state) VALUES(%s,%s,%s,now()+interval '2 days','live')",(key,digest,len(payload)))
        c.execute("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(%s,'workflow',%s,%s)",(key,flow,kind))
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s,%s,%s,%s)",(uuid4(),task,kind,key,"0"*64 if case=="wrong_digest" else digest,len(payload)))
        if case=="shared":
            other=uuid4()
            c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','created','python')",(other,))
            c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s,%s,%s,%s)",(uuid4(),other,kind,key,digest,len(payload)))
    legacy.append((case,task,flow,key))
ttl(1);gc()
for case,task,flow,key in legacy:
    with db() as c:
        refs=c.execute("SELECT count(*) FROM storage_object_refs WHERE storage_key=%s",(key,)).fetchone()[0]
        assert refs==(1 if case in ("shared","wrong_digest","unknown") else 0),(case,refs)
        c.execute("UPDATE storage_objects SET retain_until=now()-interval '1 minute' WHERE storage_key=%s",(key,))
    if case=="changed": (root/key).write_bytes(b"tampered")
gc()
for case,task,flow,key in legacy:
    if case in ("audio","source","translated","cover"): deleted((task,flow,key))
    else: retained((task,flow,key))
print("PASS: registered original work/sub/final paths released and physically collected, shared/unknown/digest-mismatch/tampered files retained")

# A failed cleanup cannot invalidate a confirmed publication. Backfill its
# retention without replaying the publish stage; ambiguous receipts stay held.
for confirmed in (True,False):
    item=seed("published",72)
    task,flow,key=item
    with db() as c:
        c.execute("UPDATE vr2_workflows SET state='failed',current_stage='cleanup' WHERE id=%s",(flow,))
        j=uuid4()
        c.execute("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,result,attempts,max_attempts,finished_at) VALUES(%s,%s,%s,'publish',1,%s,'succeeded',%s::jsonb,1,1,now())",(j,flow,task,"retention-fixture:"+str(j),json.dumps({"confirmed":confirmed,"bvid":"BV1TESTONLY","external_id":"BV1TESTONLY"})))
    gc()
    check(item,confirmed)
    with db() as c:
        assert c.execute("SELECT state FROM vr2_jobs WHERE id=%s",(j,)).fetchone()[0]=="succeeded"
print("PASS: confirmed publication with failed cleanup receives retention backfill; ambiguous result is retained")
