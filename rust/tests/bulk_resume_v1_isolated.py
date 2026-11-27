"""Original recent-failed recovery using historical task/asset/job evidence."""
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import uuid4
import psycopg

uri=os.environ["DATABASE_URL"]
assert urlsplit(uri).hostname=="127.0.0.1" and urlsplit(uri).path=="/videoroll_test"
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name=="v2-smoke-objects"
fixture=root/("bulk-resume-fixture-"+str(uuid4()))
fixture.mkdir()
video=fixture/"sample.mp4"
subprocess.run(["ffmpeg","-hide_banner","-nostdin","-loglevel","error","-f","lavfi","-i","color=c=blue:s=160x90:r=10","-t","1","-c:v","libx264","-threads","1","-pix_fmt","yuv420p","-y",str(video)],check=True)
data=video.read_bytes()
def db():return psycopg.connect(uri)
def post(limit=200,hours=24):
    req=Request(f"http://127.0.0.1:18080/api/v1/tasks/actions/resume_failed_recent?window_hours={hours}&limit={limit}",data=b"{}",method="POST",headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"],"content-type":"application/json"})
    try:r=urlopen(req,timeout=40)
    except HTTPError as e:r=e
    with r:return r.status,json.loads(r.read())
def seed(source="youtube",owner="rust",license="own",old=False,raw=False,subtitle=False,checkpoint=False,missing=False,stage=None,job_state="failed",empty=False,url="https://youtu.be/0123456789A"):
    t=uuid4()
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner,error_code,error_message,updated_at) VALUES(%s,%s,%s,%s,'failed',%s,'FIXTURE','old error',now()-CASE WHEN %s THEN interval '10 days' ELSE interval '0 seconds' END)",(t,source,url if source=="youtube" else None,license,owner,old))
        key=f"raw/{t}/source.mp4"
        if raw:
            path=root/key;path.parent.mkdir(parents=True,exist_ok=True)
            if not missing:path.write_bytes(data)
            c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw',%s,%s,%s)",(uuid4(),t,key,hashlib.sha256(data).hexdigest(),len(data)))
            c.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,status,completed_at,result_json) VALUES(%s,%s,'rust-task-cutover',%s,'completed',now(),'{}')",(uuid4(),"task-ownership-cutover:"+str(t),t))
        if subtitle:
            req={"task_id":str(t),"runtime_profile":False,"input":{"type":"storage","key":key},"asr":{"engine":"auto","language":"en"},"translate":{"enabled":True,"target_lang":"zh-Hans","bilingual":True},"output":{"formats":["srt","ass"],"render":{"burn_in":True,"video_codec":"h264"}},"output_prefix":f"sub/{t}/","after_render":{"publish":True,"runtime_profile":True}}
            if checkpoint:
                content=json.dumps([{"start":0.0,"end":0.9,"text":"hello"}]).encode()
                digest=hashlib.sha256(content).hexdigest()
                skey=f"sub/{t}/segments_{digest[:16]}_0123456789ab.json"
                path=root/skey;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
                c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'segments_json',%s,%s,%s)",(uuid4(),t,skey,digest,len(content)))
                req["artifacts"]={"source_segments_key":skey}
            c.execute("INSERT INTO subtitle_jobs(id,task_id,status,request_json) VALUES(%s,%s,'failed',%s::jsonb)",(uuid4(),t,json.dumps({} if empty else req)))
        if stage:
            flow,j=uuid4(),uuid4()
            c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(%s,%s,1,'failed',%s,%s::jsonb)",(flow,t,stage,json.dumps({"auto_publish":False,"auto_dub_p2":False})))
            c.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,workflow_id,task_id,stage,generation,status,completed_at) VALUES(%s,%s,'rust-v2',%s,%s,%s,1,%s,now())",(j,"fixture:"+str(j),flow,t,stage,job_state))
    return t
def flow(t):
    with db() as c:
        return c.execute("SELECT generation,state,current_stage,configuration FROM vr2_workflows WHERE task_id=%s ORDER BY generation",(t,)).fetchall()
def state(t):
    with db() as c:return c.execute("SELECT status::text,error_code,updated_at FROM tasks WHERE id=%s",(t,)).fetchone()

no_sub=seed()
raw_only=seed(raw=True)
legacy=seed(source="local",raw=True,subtitle=True,checkpoint=True)
native=seed(stage="download")
local=seed(source="local")
license=seed(license="unknown")
bad_url=seed(url="https://example.com/video")
empty=seed(source="local",raw=True,subtitle=True,empty=True)
missing=seed(source="local",raw=True,subtitle=True,missing=True)
active=seed()
batch=seed()
receipt=seed()
publish=seed(stage="publish")
python=seed(owner="python")
expired=seed(old=True)
pending=seed()
paused=seed()
with db() as c:
    c.execute("INSERT INTO subtitle_jobs(id,task_id,status,request_json) VALUES(%s,%s,'running','{}')",(uuid4(),active))
    c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets) VALUES(%s,%s,'active','[]')",(uuid4(),batch))
    c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,external_id) VALUES(%s,%s,'bilibili','published','BVfixture')",(uuid4(),receipt))
    c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json) VALUES(%s,%s,'processing',%s::jsonb)",(uuid4(),"youtube-download:"+str(pending),json.dumps({"task_id":str(pending)})))
    c.execute("UPDATE tasks SET stopped_status='downloaded' WHERE id=%s",(paused,))
protected=[local,license,bad_url,empty,missing,active,batch,receipt,publish,python,expired,pending,paused]
before={t:(state(t),flow(t)) for t in protected}
def original():
    with db() as c:return {table:c.execute("SELECT to_jsonb(x) FROM "+table+" x ORDER BY 1").fetchall() for table in ("assets","subtitle_jobs","publish_jobs","publish_batches","outbox_events")}
saved=original()
# A table writer defers rather than blocking or claiming.
with db() as c:
    c.execute("LOCK TABLE publish_jobs IN ROW EXCLUSIVE MODE")
    code,body=post()
    assert code==200 and body["resumed_count"]==0 and body["failed_count"]==0,(code,body)
assert flow(no_sub)==[]
code,body=post()
assert code==200 and body["matched_count"]==15 and body["resumed_count"]==4 and body["skipped_count"]==11 and body["failed_count"]==0,(code,body)
assert {r["task_id"] for r in body["results"] if r["status"]=="queued"}=={str(t) for t in (no_sub,raw_only,legacy,native)}
for t,stage,count in [(no_sub,"download",1),(raw_only,"subtitle",1),(legacy,"subtitle",1),(native,"download",2)]:
    rows=flow(t)
    assert len(rows)==count and rows[-1][1:3]==("running",stage),rows
    assert rows[-1][3]["auto_publish"] is False
    assert state(t)[1] is None
config=flow(legacy)[0][3]
assert config["subtitle_resume_plan"]["source"]["key"].startswith("sub/")
assert config["subtitle"]["bilingual"] is True and config["auto_dub_p2"] is False
assert original()==saved
assert all((state(t),flow(t))==before[t] for t in protected)
assert post()[1]["resumed_count"]==0
# Concurrent bulk requests cannot create multiple generations for one task.
race=seed()
with ThreadPoolExecutor(max_workers=4) as pool:
    answers=list(pool.map(lambda _:post(),range(4)))
assert all(code==200 for code,_ in answers),answers
if not flow(race):post()
assert len(flow(race))==1
assert sum(r["resumed_count"] for _,r in answers)<=1
assert original()==saved
print("PASS: historical subtitle request and checkpoint, YouTube without native workflow or subtitle job, raw reuse, native stage recovery, original result counts, ownership/license/active/receipt guards, nonblocking ledger contention and concurrent idempotency")
