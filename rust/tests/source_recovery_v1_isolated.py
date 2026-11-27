"""Native missing-source recovery: original receipts, concurrent API calls and real Worker."""
import hashlib
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import create_engine, text

uri=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in uri and uri.endswith("/videoroll_test")
engine=create_engine(uri.replace("postgresql://","postgresql+psycopg://",1))
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name=="v2-smoke-objects"
base="http://127.0.0.1:18080/api/v1"
headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"],
         "content-type":"application/json"}
def post(task,action="youtube_download"):
    req=Request(f"{base}/tasks/{task}/actions/{action}",data=b"{}",method="POST",headers=headers)
    try: response=urlopen(req,timeout=20)
    except HTTPError as error: response=error
    with response:
        return response.status,json.loads(response.read())
def query(sql, **params):
    with engine.connect() as db:
        return db.execute(text(sql),params).mappings().all()

fixture=root/f"source-recovery-fixture-{uuid4()}"
fixture.mkdir()
video=fixture/"source.mp4"
different=fixture/"different.mp4"
for path,color in [(video,"blue"),(different,"red")]:
    subprocess.run(["ffmpeg","-hide_banner","-nostdin","-loglevel","error",
        "-f","lavfi","-i",f"color=c={color}:s=160x90:r=10",
        "-t","1","-c:v","libx264","-threads","1","-pix_fmt","yuv420p","-y",str(path)],check=True)
payload=video.read_bytes()
sha=hashlib.sha256(payload).hexdigest()
size=len(payload)

def seed(label,*,checksum=True,flow_state="failed",history=False,receipt=True,task_state="failed",legacy=False):
    task,flow,job,asset=(str(uuid4()) for _ in range(4))
    key=f"v2/raw/{task}/native/source.mp4"
    result={"storage_key":key,"sha256":sha,"size_bytes":size} if receipt else {}
    with engine.begin() as db:
        db.execute(text("""INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner,priority)
            VALUES(CAST(:task AS uuid),'youtube','https://youtu.be/0123456789A','authorized',CAST(:status AS task_status),'rust',100)"""),
            {"task":task,"status":task_state})
        db.execute(text("""INSERT INTO vr2_workflows(id,task_id,state,generation,current_stage,configuration)
            VALUES(CAST(:flow AS uuid),CAST(:task AS uuid),:state,1,'download',CAST(:config AS jsonb))"""),
            {"flow":flow,"task":task,"state":flow_state,"config":json.dumps({"auto_publish":True,"after_render_publish":{"platform":"bilibili"}})})
        db.execute(text("""INSERT INTO operation_inbox(id,operation_key,runtime_engine,workflow_id,task_id,stage,generation,status,result_json,completed_at)
            VALUES(CAST(:job AS uuid),:op,'rust-v2',CAST(:flow AS uuid),CAST(:task AS uuid),'download',1,'succeeded',CAST(:result AS jsonb),now())"""),
            {"job":job,"op":f"recovery-test:{job}","flow":flow,"task":task,"result":json.dumps(result)})
        db.execute(text("""INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes)
            VALUES(CAST(:asset AS uuid),CAST(:task AS uuid),'video_raw',:key,:sha,:size)"""),
            {"asset":asset,"task":task,"key":key,"sha":sha if checksum else None,"size":size})
        if history:
            db.execute(text("""INSERT INTO publish_jobs(id,task_id,platform,state,bvid,external_id)
                VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'bilibili','published','BVfixture','BVfixture')"""),{"id":str(uuid4()),"task":task})
        if legacy:
            db.execute(text("INSERT INTO operation_inbox(id,operation_key,status) VALUES(CAST(:id AS uuid),:key,'processing')"),
                       {"id":str(uuid4()),"key":"youtube-download:"+task})
    return {"label":label,"task":task,"flow":flow,"job":job,"asset":asset,"key":key}

def generations(case):
    return query("SELECT id,state,generation,request_json FROM pipeline_runs WHERE task_id=CAST(:task AS uuid) ORDER BY generation",task=case["task"])
def jobs(case):
    return query("SELECT id,stage,status,last_error,result_json,generation,attempt_count FROM operation_inbox WHERE task_id=CAST(:task AS uuid) AND runtime_engine='rust-v2' ORDER BY generation,stage",task=case["task"])
def verify_history(case):
    assert query("SELECT id,storage_key,sha256 FROM assets WHERE task_id=CAST(:task AS uuid)",task=case["task"])[0]["id"].hex==case["asset"].replace("-","")
    assert jobs(case)[0]["status"]=="succeeded" and str(jobs(case)[0]["id"])==case["job"]
def queue(case):
    code,response=post(case["task"])
    assert code==409 and response["detail"]["code"]=="YOUTUBE_DOWNLOAD_IN_PROGRESS",(case,code,response)
    rows=generations(case)
    assert len(rows)==2 and rows[1]["state"]=="running",rows
    config=rows[1]["request_json"]
    assert config["auto_publish"] is False and config["auto_dub_p2"] is False and "after_render_publish" not in config,config
    assert config["source_recovery"]["asset_id"]==case["asset"],config
    return str(rows[1]["id"])

good=seed("verified")
bad=seed("different bytes")
blocked=[seed("no hash",checksum=False),seed("paused",flow_state="paused"),
         seed("publication",history=True),seed("no receipt identity",receipt=False),
         seed("published",task_state="published"),seed("legacy lease",legacy=True)]
try:
    for case in blocked:
        code,response=post(case["task"])
        assert code==409,(case,code,response)
        code,response=post(case["task"],"youtube_download_enqueue")
        assert code==409,(case,code,response)
        assert len(generations(case))==1 and len(jobs(case))==1,case
        verify_history(case)
    assert query("SELECT external_id FROM publish_jobs WHERE task_id=CAST(:task AS uuid)",task=blocked[2]["task"])[0]["external_id"]=="BVfixture"
    # Concurrent requests must queue exactly one independent generation.
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses=list(pool.map(lambda i:post(good["task"],"youtube_download_enqueue" if i%2 else "youtube_download"),range(8)))
    assert all(code in (200,409) for code,_ in responses),responses
    assert all((code==200 and body["status"]=="queued") or
               (code==409 and body["detail"]["code"]=="YOUTUBE_DOWNLOAD_IN_PROGRESS") for code,body in responses),responses
    flow=queue(good)
    bad_flow=queue(bad)
    assert len(jobs(good))==2 and len(jobs(bad))==2
    # Emulate a completed download left in the generation-specific directory
    # by a crashed child process. Real Worker/FFprobe/hash/settlement run below.
    for case,gen,sample in [(good,flow,video),(bad,bad_flow,different)]:
        staging=root/f"v2/raw/{case['task']}/recovery-{gen}/source.mp4"
        staging.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(sample,staging)
    exe=Path(__file__).resolve().parents[1]/"target"/"debug"/"videoroll-v2"
    env=os.environ.copy()
    env.update({"VIDEOROLL_V2_WORKERS":"1","VIDEOROLL_V2_YT_DLP":"/bin/false",
                "VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI":"false","VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH":"false",
                "VIDEOROLL_V2_ENABLE_SAU_PUBLISH":"false","VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP":"false"})
    log=fixture/"worker.log"
    with log.open("w") as output:
        worker=subprocess.Popen([str(exe),"worker"],env=env,stdout=output,stderr=subprocess.STDOUT)
        try:
            for _ in range(80):
                g,b=jobs(good),jobs(bad)
                if g[-1]["status"]=="succeeded" and b[-1]["last_error"]:
                    break
                if worker.poll() is not None:
                    raise RuntimeError(log.read_text())
                time.sleep(.25)
            else: raise AssertionError((jobs(good),jobs(bad),log.read_text()[-6000:]))
        finally:
            worker.terminate()
            try: worker.wait(timeout=10)
            except subprocess.TimeoutExpired: worker.kill();worker.wait()
    assert len(jobs(good))==2 and generations(good)[-1]["state"]=="succeeded"
    assert jobs(good)[-1]["result_json"]["sha256"]==sha
    assert (root/good["key"]).read_bytes()==payload
    assert query("SELECT status::text AS status FROM tasks WHERE id=CAST(:task AS uuid)",task=good["task"])[0]["status"]=="downloaded"
    assert len(jobs(bad))==2 and "differs from historical asset" in jobs(bad)[-1]["last_error"]
    assert not (root/bad["key"]).exists()
    for case in (good,bad):
        verify_history(case)
        assert not query("SELECT id FROM publish_jobs WHERE task_id=CAST(:task AS uuid)",task=case["task"])
    assert post(good["task"],"youtube_download_enqueue")== (200,{"task_id":good["task"],"status":"completed"})
    print("PASS: concurrent recovery, immutable old receipt/asset, real Worker exact hash restoration, download-only settlement, mismatch protection and publication fences")
finally:
    for case in [good,bad,*blocked]:
        shutil.rmtree(root/f"v2/raw/{case['task']}",ignore_errors=True)
    shutil.rmtree(fixture,ignore_errors=True)
