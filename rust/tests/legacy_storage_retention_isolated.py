"""Scheduled legacy retention on a disposable database and storage root."""
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import psycopg

uri=os.environ["DATABASE_URL"]
parsed=urlsplit(uri)
assert parsed.hostname=="127.0.0.1" and parsed.path=="/videoroll_test"
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name=="v2-smoke-objects"
lock="orchestrator.storage_resource_cleanup"
def db():
    return psycopg.connect(uri)
def tick(manual=True, scheduled=True, hours=48):
    env=os.environ.copy()
    env.update(VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP=str(manual).lower(),
        VIDEOROLL_V2_ALLOW_V1_SCHEDULED_CLEANUP=str(scheduled).lower(),
        FAILED_TASK_RESOURCE_RETENTION_HOURS=str(hours))
    subprocess.run(["./target/debug/videoroll-v2","legacy-storage-tick"],env=env,check=True,timeout=90)
def task(status,age=72,stopped=None):
    t=uuid4()
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,stopped_status,runtime_owner,updated_at) VALUES(%s,'local','own',%s,%s,'python',now()-(%s*interval '1 hour'))",(t,status,stopped,age))
    return t
def file(t,name="video.mp4",prefix="raw",asset=True,fresh=False):
    key=f"{prefix}/{t}/{name}"
    p=root/key
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_bytes(b"legacy fixture")
    if not fresh:
        stamp=time.time()-3600*400
        os.utime(p,(stamp,stamp))
    if asset:
        with db() as c:
            c.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_raw',%s)",(uuid4(),t,key))
    return key
def count(key):
    with db() as c:
        return c.execute("SELECT count(*) FROM assets WHERE storage_key=%s",(key,)).fetchone()[0]
def keep(key,refs=1):
    assert (root/key).is_file() and count(key)==refs,(key,count(key))
def gone(key):
    assert not (root/key).exists() and count(key)==0,(key,count(key))
def settings(days):
    with db() as c:
        c.execute("INSERT INTO app_settings(key,value_json) VALUES('storage.retention',%s::jsonb) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({"asset_ttl_days":days}),))
def lock_value():
    with db() as c:
        r=c.execute("SELECT value_json FROM app_settings WHERE key=%s",(lock,)).fetchone()
        return r[0] if r else {}
def seed_lock(value):
    with db() as c:
        c.execute("INSERT INTO app_settings(key,value_json) VALUES(%s,%s::jsonb) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(lock,json.dumps(value)))

with db() as c:
    c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,updated_at) SELECT md5('legacy-retention-empty-'||i)::uuid,'local','own','failed','python',now()-interval '200 hours' FROM generate_series(1,128) i")
old=task("failed")
old_key=file(old)
orphan=file(old,"unregistered.mp4",asset=False)
recent=file(task("failed",24))
published=file(task("published"))
canceled=file(task("canceled"))
paused=file(task("canceled",72,"rendered"))
active=file(task("created",100))
fresh=file(task("failed"),fresh=True)
native=file(task("failed"),prefix="v2/raw")
custom=file(task("failed"),prefix="other")
shared=file(task("failed"))
registered=file(task("failed"))
protected_task=task("failed")
protected=file(protected_task)
other=task("created")
with db() as c:
    c.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_raw',%s)",(uuid4(),other,shared))
    c.execute("INSERT INTO storage_objects(storage_key,size_bytes,state,retain_until) VALUES(%s,14,'live',now()+interval '30 days')",(registered,))
    c.execute("INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state) VALUES(%s,%s,'hatchet','fixture','unknown')",(uuid4(),protected_task))
settings(0)
tick(manual=False)
tick(scheduled=False)
keep(old_key)
assert not lock_value().get("rust_scan_after_id")
from datetime import datetime, timedelta, timezone
seed_lock({"lock_owner":"python:running","lock_until":(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat()})
tick()
keep(old_key)
assert lock_value()["lock_owner"]=="python:running"
seed_lock({})
tick()
keep(old_key)
assert lock_value().get("rust_scan_after_id"),lock_value()
assert lock_value().get("rust_scan_next_due_at")
tick()
gone(old_key)
gone(orphan)
assert not lock_value().get("rust_scan_after_id"),lock_value()
for key in (recent,published,canceled,paused,active,fresh,native,custom,registered,protected):
    keep(key)
keep(shared,2)
print("PASS: separate opt-ins, original Python lease, bounded persistent cursor, orphan recovery and protected assets",flush=True)
settings(1)
tick(hours=12)
tick(hours=12)
for key in (recent,published,canceled):
    gone(key)
for key in (paused,active,fresh,native,custom,registered,protected):
    keep(key)
keep(shared,2)
print("PASS: original failed-hour and published/canceled-day policy; persistent cursor wrap",flush=True)
