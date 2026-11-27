#!/usr/bin/env python3
"""Terminal resource cleanup: original-table/DTO, cross-runtime lock and filesystem safety."""
import json,os,subprocess,shutil,uuid
from pathlib import Path
from datetime import datetime,timedelta,timezone
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from videoroll.apps.orchestrator_api.services.maintenance_service import task_id_from_resource_key

root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert "/rust/target/v2-smoke-objects" in root.as_posix(),root
base="http://127.0.0.1:18080/api/v1"
container=os.environ["container_name"]
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
def pg(sql,query=False):
    argv=["docker","exec","-i",container,"psql","-v","ON_ERROR_STOP=1","-At",
          "-U","postgres","-d","videoroll_test"]
    if query:
        return subprocess.check_output(argv+["-c",sql],text=True).strip()
    subprocess.run(argv,input=sql,text=True,check=True,stdout=subprocess.DEVNULL)
def call(method,path,authenticated=True):
    headers={"x-videoroll-v2-internal-token":token} if authenticated else {}
    request=Request(base+path,method=method,headers=headers)
    try:
        with urlopen(request,timeout=60) as reply:
            return reply.status,json.loads(reply.read())
    except HTTPError as err:
        return err.code,json.loads(err.read())
def id_for(index):
    return uuid.UUID(f"00000000-0000-0000-0000-{93000+index:012d}")
pub,failed,canceled,paused,live,dubbing,shared,unsafe=(id_for(x) for x in range(1,9))
pending_task=id_for(9)
paused_pipeline,unknown_pipeline,accepted_operation=(id_for(x) for x in range(10,13))
asset_pending=id_for(26)
asset1,asset2,asset3,asset_shared,asset_other=(id_for(x) for x in range(21,26))
sub_id=id_for(30)
pg(f"""INSERT INTO tasks(id,source_type,source_license,status,stopped_status) VALUES
 ('{pub}','local','own','published',NULL),
 ('{failed}','local','own','failed',NULL),
 ('{canceled}','local','own','canceled',NULL),
 ('{paused}','local','own','canceled','created'),
 ('{live}','local','own','created',NULL),
 ('{dubbing}','local','own','published',NULL),
 ('{shared}','local','own','published',NULL),
 ('{unsafe}','local','own','published',NULL),
 ('{pending_task}','local','own','failed',NULL),
 ('{paused_pipeline}','local','own','failed',NULL),
 ('{unknown_pipeline}','local','own','failed',NULL),
 ('{accepted_operation}','local','own','failed',NULL);
 INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state) VALUES
 ('{id_for(51)}','{paused_pipeline}','hatchet','fixture','paused'),
 ('{id_for(52)}','{unknown_pipeline}','hatchet','fixture','unknown');
 INSERT INTO operation_inbox(id,operation_key,task_id,runtime_engine,status) VALUES
 ('{id_for(53)}','fixture:terminal-cleanup:{accepted_operation}','{accepted_operation}','python','accepted');
 INSERT INTO assets(id,task_id,kind,storage_key) VALUES
 ('{asset1}','{pub}','video_raw','raw/{pub}/video.mp4'),
 ('{asset2}','{pub}','subtitle_srt','sub/{pub}/zh.srt'),
 ('{asset3}','{failed}','metadata_json','custom/{failed}/info.json'),
 ('{asset_shared}','{shared}','video_final','final/{shared}/shared.mp4'),
 ('{asset_other}','{live}','video_final','final/{shared}/shared.mp4'),
 ('{asset_pending}','{pending_task}','log','meta/{pending_task}/not-a-regular-file');
 INSERT INTO subtitles(id,task_id,version,format,language,storage_key)
 VALUES('{sub_id}','{pub}',1,'srt','zh','sub/{pub}/zh.srt');
 INSERT INTO dubbing_jobs(id,task_id,status,request_json)
 VALUES('{id_for(50)}','{dubbing}','queued','{{"model_path":"youdub:voxcpm2"}}'::jsonb);""")
# 140 previously cleaned terminal tasks force keyset pagination beyond the
# first 128 rows. Already-cleaned tasks must not permanently starve real work.
historical=",".join(
    "('%s','local','own','published',NULL,now()-interval '7 days')" % id_for(100+i)
    for i in range(140))
pg("INSERT INTO tasks(id,source_type,source_license,status,stopped_status,updated_at) VALUES "+historical+";")
# Asset registered as a directory: FileStore.unlink cannot remove a directory.
# Rust must queue the same original pending deletion key, not claim success.
(root/"meta"/str(pending_task)/"not-a-regular-file").mkdir(parents=True,exist_ok=True)
positive={
    f"raw/{pub}/video.mp4":b"raw-video",
    f"sub/{pub}/zh.srt":b"1\n00:00:00,000 --> 00:00:01,000\nhello",
    f"custom/{failed}/info.json":b'{"ok":true}',
    f"final/{failed}/orphan.mp4":b"orphan-from-legacy-cleaner",
    f"log/{canceled}/last.log":b"terminal log"}
retained={
    f"raw/{paused}/paused.mp4":b"resumable",
    f"raw/{live}/live.mp4":b"working",
    f"dub/{dubbing}/audio.wav":b"active dubbing",
    f"final/{shared}/shared.mp4":b"other task owns shared file",
    f"raw/{paused_pipeline}/paused.mp4":b"paused pipeline still owns media",
    f"raw/{unknown_pipeline}/unknown.mp4":b"unknown pipeline may still own media",
    f"raw/{accepted_operation}/accepted.mp4":b"accepted operation still owns media"}
for key,payload in (positive|retained).items():
    file=root/key
    file.parent.mkdir(parents=True,exist_ok=True)
    file.write_bytes(payload)
    parts=key.split("/")
    if parts[0] in ("raw","final","sub","log","dub"):
        assert task_id_from_resource_key(key)==uuid.UUID(parts[1])
external=root.parent/f"terminal-cleanup-external-{unsafe}.txt"
external.write_text("never delete outside STORAGE_ROOT")
unsafe_dir=root/"raw"/str(unsafe)
unsafe_dir.mkdir(parents=True,exist_ok=True)
link=unsafe_dir/"external-link"
link.symlink_to(external)
assert call("POST","/maintenance/storage/cleanup-terminal",False)[0]==401
future=(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat()
pg(f"""INSERT INTO app_settings(key,value_json)
 VALUES('orchestrator.storage_resource_cleanup',
 '{{"lock_owner":"python:currently-running","lock_until":"{future}"}}'::jsonb)
 ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json;""")
locked,_=call("POST","/maintenance/storage/cleanup-terminal")
assert locked==409,locked
assert all((root/key).exists() for key in positive|retained)
pg("""UPDATE app_settings SET value_json='{}'::jsonb
 WHERE key='orchestrator.storage_resource_cleanup';""")
code,result=call("POST","/maintenance/storage/cleanup-terminal")
assert code==200,(code,result)
expected={"matched_tasks":4,"matched_assets":4,"matched_subtitles":1,
    "deleted_assets":4,"deleted_subtitles":1,"deleted_objects":5,"pending_objects":1}
assert result==expected,(result,expected)
assert all(not (root/key).exists() for key in positive)
assert all((root/key).read_bytes()==payload for key,payload in retained.items())
assert external.read_text()=="never delete outside STORAGE_ROOT"
assert link.is_symlink()
assert not pg("SELECT COALESCE(value_json->>'lock_owner','') FROM app_settings WHERE key='orchestrator.storage_resource_cleanup'",True)
assert pg(f"SELECT COUNT(*) FROM tasks WHERE id IN ('{pub}','{failed}','{canceled}')",True)=="3"
assert pg(f"SELECT COUNT(*) FROM assets WHERE task_id IN ('{pub}','{failed}')",True)=="0"
assert pg(f"SELECT COUNT(*) FROM subtitles WHERE task_id='{pub}'",True)=="0"
assert pg(f"SELECT COUNT(*) FROM assets WHERE task_id='{shared}'",True)=="1"
assert pg(f"SELECT COUNT(*) FROM dubbing_jobs WHERE task_id='{dubbing}'",True)=="1"
import hashlib
bad_key=f"meta/{pending_task}/not-a-regular-file"
pending_hash=hashlib.sha256(("filesystem\0"+bad_key).encode()).hexdigest()
pending_setting="storage.pending_delete."+pending_hash
queued=pg("SELECT value_json->>'storage_key' FROM app_settings WHERE key='"+pending_setting+"'",True)
assert queued==bad_key,queued
assert (root/bad_key).is_dir()

code,again=call("POST","/maintenance/storage/cleanup-terminal")
assert code==200 and again["deleted_objects"]==0 and again["deleted_assets"]==0,again
print("PASS: terminal cleanup reuses original assets/subtitles, orphans and Python lock; blocks paused, active, shared, symlink and dubbing files")
print("PASS: task history remains, pending counts honest, second call idempotent and external symlink target unchanged")

# Exercise the actual native GC command and original Python pending-delete keys.
# The original test script starts only the isolated API, no competing workers.
old_request=datetime.now(timezone.utc)-timedelta(minutes=20)
def queue(key,bucket="filesystem",fake_hash=False):
    digest=hashlib.sha256(((bucket or "")+"\0"+key).encode()).hexdigest()
    setting="storage.pending_delete."+("0"*64 if fake_hash else digest)
    payload=json.dumps({"storage_key":key,"bucket":bucket,
        "reason":"manual_asset_delete","requested_at":old_request.isoformat()})
    pg(f"INSERT INTO app_settings(key,value_json,updated_at) VALUES('{setting}','{payload}'::jsonb,now()-interval '20 minutes')")
    return setting
def fixture(key,old=True):
    path=root/key
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b"pending fixture")
    if old:
        stamp=(old_request-timedelta(minutes=1)).timestamp()
        os.utime(path,(stamp,stamp))
    return path
retry_key=f"raw/{failed}/retry.mp4"
missing_key=f"raw/{failed}/already-gone.mp4"
new_key=f"raw/{failed}/replacement.mp4"
active_key=f"raw/{live}/pending-active.mp4"
ref_key=f"raw/{failed}/referenced.mp4"
native_key=f"v2/raw/{failed}/untouched.mp4"
bad_hash_key=f"raw/{failed}/bad-hash.mp4"
link_key=f"raw/{unsafe}/external-link"
retry_file=fixture(retry_key)
new_file=fixture(new_key,False)
active_file=fixture(active_key)
ref_file=fixture(ref_key)
native_file=fixture(native_key)
bad_hash_file=fixture(bad_hash_key)
pg(f"INSERT INTO assets(id,task_id,kind,storage_key) VALUES('{id_for(54)}','{live}','video_raw','{ref_key}')")
completed=[queue(retry_key,None),queue(missing_key)]
protected=[queue(new_key),queue(active_key),queue(ref_key),queue(native_key),queue(bad_hash_key,fake_hash=True),queue(link_key)]
pg(f"UPDATE app_settings SET updated_at=now()-interval '20 minutes' WHERE key='{pending_setting}'")
gc_exe=Path(__file__).resolve().parents[1]/"target"/"debug"/"videoroll-v2"
def gc(enabled):
    env=os.environ.copy()
    env["VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP"]="true" if enabled else "false"
    subprocess.run([str(gc_exe),"gc","--limit","100"],env=env,check=True)
def pending_exists(setting):
    return pg(f"SELECT count(*) FROM app_settings WHERE key='{setting}'",True)=="1"
gc(False)
assert retry_file.is_file() and all(pending_exists(s) for s in completed)
gc(True)
assert not retry_file.exists() and not any(pending_exists(s) for s in completed)
assert all(pending_exists(s) for s in protected+[pending_setting])
assert all(p.is_file() for p in (new_file,active_file,ref_file,native_file,bad_hash_file))
assert link.is_symlink() and external.read_text()=="never delete outside STORAGE_ROOT"
gc(True)
assert all(pending_exists(s) for s in protected+[pending_setting])
assert all(p.is_file() for p in (new_file,active_file,ref_file,native_file,bad_hash_file))
print("PASS: native pending delete GC respects opt-in, original hash, missing files, active/shared refs, replacements, symlinks and v2 ownership")
for path in (new_file,active_file,ref_file,native_file,bad_hash_file):
    path.unlink()
link.unlink()
shutil.rmtree(unsafe_dir)
external.unlink()
for key in retained:
    (root/key).unlink()
shutil.rmtree(root/"meta"/str(pending_task))
for task in (paused,live,dubbing,shared,paused_pipeline,unknown_pipeline,accepted_operation):
    for prefix in ("raw","final","dub"):
        parent=root/prefix/str(task)
        if parent.exists():
            shutil.rmtree(parent)
