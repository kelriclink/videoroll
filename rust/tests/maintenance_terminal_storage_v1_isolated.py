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
 ('{pending_task}','local','own','failed',NULL);
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
    f"final/{shared}/shared.mp4":b"other task owns shared file"}
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
link.unlink()
shutil.rmtree(unsafe_dir)
external.unlink()
for key in retained:
    (root/key).unlink()
shutil.rmtree(root/"meta"/str(pending_task))
for task in (paused,live,dubbing,shared):
    for prefix in ("raw","final","dub"):
        parent=root/prefix/str(task)
        if parent.exists():
            shutil.rmtree(parent)
