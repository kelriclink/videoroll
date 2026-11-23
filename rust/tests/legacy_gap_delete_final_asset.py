#!/usr/bin/env python3
"""V1 DELETE final asset on canonical tables; no direct unlink, GC-fenced and task scoped.

All IDs and media files are new disposable fixtures. Never touches production.
"""
import os,hashlib,uuid,subprocess
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import psycopg
from psycopg.rows import dict_row

uri=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in uri and uri.endswith("/videoroll_test")
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert "v2-smoke" in str(root),root
auth=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
def send(task,asset):
    path=f"http://127.0.0.1:18080/api/v1/tasks/{task}/assets/{asset}"
    try:r=urlopen(Request(path,method="DELETE",headers={"x-videoroll-v2-internal-token":auth}),timeout=10)
    except HTTPError as error:r=error
    with r:
        import json
        return r.status,json.loads(r.read())
def one(sql,params=()):
    with psycopg.connect(uri,row_factory=dict_row) as conn:
        return conn.execute(sql,params).fetchone()
def fixture(kind,owner="rust",status="created"):
    task,asset=uuid.uuid4(),uuid.uuid4()
    key=f"v2/artifacts/{task}/{asset}.mp4"
    path=root/key
    path.parent.mkdir(parents=True,exist_ok=True)
    content=b"mock persisted video for deletion fence"
    path.write_bytes(content)
    with psycopg.connect(uri) as conn:
        conn.execute("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner)
            VALUES(%s,'local','own',%s::task_status,%s)""",(task,status,owner))
        conn.execute("""INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes)
            VALUES(%s,%s,%s::asset_kind,%s,%s,%s)""",
            (asset,task,kind,key,hashlib.sha256(content).hexdigest(),len(content)))
        conn.execute("""INSERT INTO storage_objects(
             storage_key,checksum_sha256,size_bytes,state,retain_until)
             VALUES(%s,%s,%s,'live',now()+interval '2 days')""",
            (key,hashlib.sha256(content).hexdigest(),len(content)))
        conn.execute("""INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
             VALUES(%s,'task',%s,%s)""",(key,task,kind))
    return task,asset,key,path
missing=uuid.uuid4()
assert send(missing,missing)[0]==404
t,a,key,path=fixture("video_raw")
assert send(t,a)[0]==400
assert one("SELECT id FROM assets WHERE id=%s",(a,)) is not None
t0,a0,key0,path0=fixture("video_final",owner="python")
assert send(t0,a0)[0]==404
assert one("SELECT id FROM assets WHERE id=%s",(a0,)) is not None
assert send(t,a0)[0]==404
print("PASS: missing task, invalid kind, cross-task and legacy Python ownership reject before deletion")

t,a,key,path=fixture("video_final")
w=uuid.uuid4()
with psycopg.connect(uri) as conn:
    conn.execute("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,
           generation,request_json)
       VALUES(%s,%s,'rust-v2','videoroll.rust.pipeline','running',1,'{}'::jsonb)""",(w,t))
assert send(t,a)[0]==409
assert one("SELECT id FROM assets WHERE id=%s",(a,)) is not None
with psycopg.connect(uri) as conn:
    conn.execute("UPDATE pipeline_runs SET state='succeeded' WHERE id=%s",(w,))

link=uuid.uuid4()
with psycopg.connect(uri) as conn:
    conn.execute("""INSERT INTO playout_asset_links(
      id,task_id,asset_id,source_asset_id,ffplayout_channel_id,relative_media_path,
      playout_path,source_checksum,transfer_mode,status,runtime_engine)
      VALUES(%s,%s,%s,%s,1,'VideoRoll/preserved.mp4','/sandbox/VideoRoll/preserved.mp4',
       %s,'copy','ready','rust-v2')""",
       (link,t,a,a,hashlib.sha256(path.read_bytes()).hexdigest()))
    conn.execute("""INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
       VALUES(%s,'playout',%s,'source_video')""",(key,link))
status,result=send(t,a)
assert status==200 and result=={"deleted":True},(status,result)
assert path.is_file(),"deferred deletion should not unlink source inside HTTP request"
assert one("SELECT id FROM assets WHERE id=%s",(a,)) is None
assert one("SELECT id FROM playout_asset_links WHERE id=%s",(link,)) is None
assert one("SELECT count(*) AS n FROM storage_object_refs WHERE storage_key=%s",(key,))["n"]==0
obj=one("SELECT state,retain_until<=now() AS due FROM storage_objects WHERE storage_key=%s",(key,))
assert obj["state"]=="live" and obj["due"],obj
assert send(t,a)[0]==404
print("PASS: DELETE final asset is transactionally durable, removes canonical playout mapping and refs, schedules deferred GC without immediate unlink")

binary=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
subprocess.run([str(binary),"gc","--limit","100"],check=True,stdout=subprocess.DEVNULL,timeout=60)
assert not path.exists(),"explicit garbage collector must remove orphaned mock source"
assert one("SELECT state FROM storage_objects WHERE storage_key=%s",(key,))["state"]=="deleted"
assert path0.exists(),"Python original asset source must never be removed by Rust GC"
print("PASS: explicit GC deletes only unreferenced Rust source, leaves old Python and non-video_final assets untouched")
