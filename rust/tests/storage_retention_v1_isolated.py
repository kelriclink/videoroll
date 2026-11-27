"""Exercise terminal retention scheduling using the actual Rust GC and disposable PG."""
import json
import os
import subprocess
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
    subprocess.run(["./target/debug/videoroll-v2","gc","--limit","50"],env=env,check=True)
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
