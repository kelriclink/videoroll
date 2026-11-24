#!/usr/bin/env python3
"""Original-schema workdir scanner differential and cleanup safety, disposable fixtures."""
import json,os,subprocess,uuid,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from videoroll.utils.workdir_maintenance import WorkdirJobState,scan_workdir

root=Path(os.environ["VIDEOROLL_V2_WORK_DIR"])
root.mkdir(parents=True,exist_ok=True)
assert "rust/target/v2-smoke-workdir" in root.as_posix(),root
base="http://127.0.0.1:18080/api/v1"
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
container=os.environ["container_name"]
def pg(sql,query=False):
    cmd=["docker","exec","-i",container,"psql","-At","-v","ON_ERROR_STOP=1","-U","postgres","-d","videoroll_test"]
    if query:return subprocess.check_output(cmd+["-c",sql],text=True).strip()
    subprocess.run(cmd,input=sql,text=True,check=True,stdout=subprocess.DEVNULL)
def request(method,path,auth=True):
    headers={"x-videoroll-v2-internal-token":token} if auth else {}
    req=Request(base+path,method=method,headers=headers)
    try:
        with urlopen(req,timeout=75) as r:return r.status,json.loads(r.read())
    except HTTPError as e:return e.code,json.loads(e.read())

task=uuid.UUID("00000000-0000-0000-0000-000000009101")
yt_active=uuid.UUID("00000000-0000-0000-0000-000000009102")
task_active=uuid.UUID("00000000-0000-0000-0000-000000009103")
subtitle_done=uuid.UUID("00000000-0000-0000-0000-000000009201")
subtitle_active=uuid.UUID("00000000-0000-0000-0000-000000009202")
subtitle_youtube=uuid.UUID("00000000-0000-0000-0000-000000009203")
render_done=uuid.UUID("00000000-0000-0000-0000-000000009301")
pg(f"""INSERT INTO tasks(id,source_type,source_license,status) VALUES
 ('{task}','local','own','created'),('{yt_active}','youtube','authorized','created'),
 ('{task_active}','local','own','created');
 INSERT INTO subtitle_jobs(id,task_id,status) VALUES
 ('{subtitle_done}','{task}','succeeded'),('{subtitle_active}','{task_active}','running'),
 ('{subtitle_youtube}','{yt_active}','running');
 INSERT INTO render_jobs(id,task_id,status) VALUES
 ('{render_done}','{task}','succeeded');""")
now=datetime.now(timezone.utc)
def folder(kind,owner,age):
    path=root/kind/str(owner)
    path.mkdir(parents=True,exist_ok=True)
    file=path/"example.bin"
    file.write_bytes(b"unit-test-file-123")
    stamp=time.time()-age
    os.utime(file,(stamp,stamp))
    os.utime(path,(stamp,stamp))
    return path
remove=[
    folder("subtitle",subtitle_done,3600),
    folder("render",render_done,3600),
    folder("youtube","invalid",3600)]
keep=[
    folder("subtitle",subtitle_active,3600),
    folder("youtube",yt_active,3600),
    folder("youtube","recent-invalid",1)]
outside=root.parent/"workdir-external-canary.txt"
outside.write_text("external file must survive")
symlink=root/"youtube"/"external-link"
symlink.symlink_to(outside)
assert request("GET","/maintenance/workdir",False)[0]==401
status,report=request("GET","/maintenance/workdir")
assert status==200,(status,report)
assert report["scanned_dirs"]==6 and report["reclaimable_dirs"]==3,report
assert report["total_bytes"]==6*len(b"unit-test-file-123")
assert report["reclaimable_bytes"]==3*len(b"unit-test-file-123")
assert report["deleted_dirs"]==0 and not report["deleted_paths"]
assert all(p.is_dir() for p in remove)
assert outside.read_text()=="external file must survive"
reference=scan_workdir(root,
    subtitle_jobs={
        subtitle_done:WorkdirJobState(task_id=task,status="succeeded"),
        subtitle_active:WorkdirJobState(task_id=task_active,status="running")},
    render_jobs={render_done:WorkdirJobState(task_id=task,status="succeeded")},
    known_task_ids={yt_active},
    active_task_ids={yt_active},
    now=now,recent_grace_seconds=900)
native={(e["kind"],e["owner_id"]):(e["reclaimable"],e["reason"],e["task_id"])
        for e in report["entries"]}
python={(e.kind,e.owner_id):(e.reclaimable,e.reason,e.task_id) for e in reference.entries}
assert native==python,(native,python)
print("PASS DIFF: Rust matches Python original workdir scanner for six fixtures")
future=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
pg(f"""INSERT INTO app_settings(key,value_json)
    VALUES('orchestrator.workdir_maintenance','{{"lock_owner":"python:active","lock_until":"{future}"}}'::jsonb)
    ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json;""")
code,err=request("POST","/maintenance/workdir/cleanup")
assert code==409,(code,err)
assert all(p.is_dir() for p in remove)
pg("""UPDATE app_settings SET value_json='{}'::jsonb
 WHERE key='orchestrator.workdir_maintenance';""")
code,clean=request("POST","/maintenance/workdir/cleanup")
assert code==200,(code,clean)
assert clean["deleted_dirs"]==3 and clean["deleted_bytes"]==3*len(b"unit-test-file-123"),clean
assert set(clean["deleted_paths"])=={p.relative_to(root).as_posix() for p in remove},clean
assert not clean["errors"],clean
assert clean["scanned_dirs"]==3 and clean["reclaimable_dirs"]==0,clean
assert all(not p.exists() for p in remove)
assert all(p.is_dir() for p in keep)
assert outside.read_text()=="external file must survive"
assert symlink.is_symlink()
assert not pg("SELECT COALESCE(value_json->>'lock_owner','') FROM app_settings WHERE key='orchestrator.workdir_maintenance'",True)
code,again=request("POST","/maintenance/workdir/cleanup")
assert code==200 and again["deleted_dirs"]==0 and again["deleted_bytes"]==0,again
print("PASS: original cross-process lock, safe deletion, retained active/recent entries, symlink avoidance and idempotency")
symlink.unlink()
outside.unlink()
