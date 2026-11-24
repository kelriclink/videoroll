"""Historical ownership migration. Disposable PostgreSQL, real tiny media, no production calls."""
import os,json,uuid,subprocess,hashlib,concurrent.futures,time
from pathlib import Path
import psycopg

url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
env={**os.environ,"VIDEOROLL_V2_NATIVE_OUTBOX_ONLY":"true","VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH":"false","VIDEOROLL_V2_COMPAT_WRITE":"false"}
def db():return psycopg.connect(url)
def run(*args,ok=True,overrides=None):
    r=subprocess.run([str(exe),*args],env={**env,**(overrides or {})},capture_output=True,text=True,timeout=45)
    assert (r.returncode==0)==ok,(args,r.returncode,r.stdout,r.stderr)
    return json.loads(r.stdout[r.stdout.index("{"):]) if ok else r.stderr
def adopt(t,*args,ok=True):return run("task-ownership-adopt","--task",str(t),"--quiesced",*args,ok=ok)
def task(status="created",source="youtube",license="own",owner="python"):
    t=uuid.uuid4()
    with db() as c:c.execute("INSERT INTO tasks(id,source_type,source_license,source_url,status,runtime_owner) VALUES(%s,%s,%s,'https://www.youtube.com/watch?v=dQw4w9WgXcQ',%s,%s)",(t,source,license,status,owner))
    return t
def owner(t):
    with db() as c:return c.execute("SELECT runtime_owner,status::text FROM tasks WHERE id=%s",(t,)).fetchone()
def workflow(t,state="running"):
    w=uuid.uuid4()
    with db() as c:c.execute("INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,external_run_id) VALUES(%s,%s,'hatchet','VideoPipelineV1',%s,'fixture-external-run')",(w,t,state))
    return w
def download(t,status="pending",attempt=0):
    with db() as c:c.execute("INSERT INTO operation_inbox(id,operation_key,status,request_json,attempt_count) VALUES(%s,%s,%s,%s,%s)",(uuid.uuid4(),f"youtube-download:{t}",status,json.dumps({"task_id":str(t)}),attempt))
def asset(t,kind,data,checksum=None,missing=False):
    a=uuid.uuid4()
    key=f"legacy/{t}/{kind}-{a}"
    p=root/key;p.parent.mkdir(parents=True,exist_ok=True)
    if not missing:p.write_bytes(data)
    with db() as c:c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s,%s,%s,%s)",(a,t,kind,key,checksum if checksum is not None else hashlib.sha256(data).hexdigest(),len(data)))
    return a,key
def publication(t,state="published",receipt="fixture-BV"):
    j=uuid.uuid4()
    with db() as c:c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,bvid,meta_json) VALUES(%s,%s,'bilibili',%s,%s,'{}')",(j,t,state,receipt))
    return j
def verify_flow(t,result,stage):
    assert owner(t)[0]=="rust"
    with db() as c:
        flow=c.execute("SELECT id,current_stage,request_json FROM pipeline_runs WHERE task_id=%s AND engine='rust-v2'",(t,)).fetchone()
        assert str(flow[0])==result["workflow_id"] and flow[1]==stage and flow[2]["auto_publish"] is False and flow[2]["auto_publish_platforms"]==[]
        assert c.execute("SELECT stage,status FROM operation_inbox WHERE task_id=%s AND runtime_engine='rust-v2'",(t,)).fetchall()==[(stage,"pending")]
        assert c.execute("SELECT count(*) FROM operation_inbox WHERE task_id=%s AND runtime_engine='rust-task-cutover'",(t,)).fetchone()[0]==1

root.mkdir(parents=True,exist_ok=True)
fixture=root/"fixture.mp4"
subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i","color=black:s=64x64:d=1","-c:v","mpeg4","-y",str(fixture)],check=True,timeout=20)
video=fixture.read_bytes()
srt=b"1\n00:00:00,000 --> 00:00:01,000\nfixture\n"
created=task();download(created)
downloaded=task("downloaded");raw_id,raw_key=asset(downloaded,"video_raw",video)
ready=task("subtitle_ready");asset(ready,"video_raw",video);asset(ready,"subtitle_srt",srt)
published=task("published");pub=publication(published)
unknown=task("failed");publication(unknown,"unknown",None)
missing_receipt=task("published");publication(missing_receipt,"published",None)
running=task("downloaded");w=workflow(running);asset(running,"video_raw",video)
failed_launch=task();launch=workflow(failed_launch,"launch_failed")
broken=task("downloaded");asset(broken,"video_raw",video,checksum="bad")
missing=task("downloaded");asset(missing,"video_raw",video,missing=True)
attempted=task();download(attempted,attempt=1)
unlicensed=task(license="unknown")
canceled=task("canceled")
finished_download=task();download(finished_download,status="completed")
review=task("ready_for_review")
native=task(owner="rust")
# Pagination is read-only and exposes unresolved tasks.
page=run("task-ownership-audit","--limit","5")
assert page["python_owned_total"]==15 and len(page["items"])==5 and page["next_after"],page
page2=run("task-ownership-audit","--after",page["next_after"],"--limit","100")
assert len(page2["items"])==10
all_items={x["task_id"]:x for x in page["items"]+page2["items"]}
assert "publication_unsettled" in all_items[str(unknown)]["blockers"]
assert "active_workflow" in all_items[str(failed_launch)]["blockers"]
assert all_items[str(created)]["resume_candidate"]=="download"
run("task-ownership-adopt","--task",str(created),ok=False)
run("task-ownership-adopt","--task",str(created),"--quiesced",ok=False,overrides={"VIDEOROLL_V2_NATIVE_OUTBOX_ONLY":"false"})
with db() as c:
    c.execute("LOCK TABLE tasks IN ROW EXCLUSIVE MODE")
    adopt(created,"--resume",ok=False)
assert owner(created)==("python","created")
adopt(created,ok=False)
a=adopt(created,"--resume")
verify_flow(created,a,"download")
with db() as c:assert c.execute("SELECT status FROM operation_inbox WHERE operation_key=%s",(f"youtube-download:{created}",)).fetchone()[0]=="canceled"
assert adopt(created,"--resume")==a
# Existing historical files and IDs remain unchanged; no redownload.
a=adopt(downloaded,"--resume")
verify_flow(downloaded,a,"subtitle")
assert a["verified_assets"][0]["asset_id"]==str(raw_id) and a["verified_assets"][0]["storage_key"]==raw_key
assert (root/raw_key).read_bytes()==video
a=adopt(ready,"--resume");verify_flow(ready,a,"render")
# Published/canceled/review states are retained and do not execute.
for t,status in ((published,"published"),(canceled,"canceled"),(review,"ready_for_review")):
    adopt(t,"--resume",ok=False)
    a=adopt(t)
    assert a["workflow_id"] is None and owner(t)==("rust",status)
    with db() as c:assert c.execute("SELECT count(*) FROM pipeline_runs WHERE task_id=%s AND engine='rust-v2'",(t,)).fetchone()[0]==0
with db() as c:assert c.execute("SELECT bvid,state FROM publish_jobs WHERE id=%s",(pub,)).fetchone()==("fixture-BV","published")
for t in (unknown,missing_receipt,attempted,failed_launch,native):
    adopt(t,ok=False)
for t in (broken,missing,unlicensed,finished_download):
    adopt(t,"--resume",ok=False)
    assert owner(t)[0]=="python"
adopt(running,"--resume",ok=False)
adopt(running,"--resume","--stopped-run",str(uuid.uuid4()),ok=False)
a=adopt(running,"--resume","--stopped-run",str(w))
verify_flow(running,a,"subtitle")
with db() as c:
    assert c.execute("SELECT state,external_run_id FROM pipeline_runs WHERE id=%s",(w,)).fetchone()==("superseded","fixture-external-run")
    assert c.execute("SELECT count(*) FROM publish_jobs").fetchone()[0]==3
    assert c.execute("SELECT count(*) FROM outbox_events").fetchone()[0]==0
# Concurrent/repeated claims return exactly one generation (NOWAIT may reject
# one competing offline request; retry returns the committed receipt).
parallel=task()
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    def try_adopt(_):
        r=subprocess.run([str(exe),"task-ownership-adopt","--task",str(parallel),"--quiesced","--resume"],env=env,capture_output=True,text=True,timeout=30)
        return r.returncode
    codes=list(pool.map(try_adopt,range(3)))
assert 0 in codes,codes
a=adopt(parallel,"--resume");verify_flow(parallel,a,"download")
print("PASS: historical task ownership, paginated audit, offline table locks, same IDs and media, resume checkpoints, old download retirement, no automatic publish, unresolved effects block, verified stopped runs, idempotent concurrent adoption",flush=True)
# Actual Rust worker executes only the restored render workflow. Other
# isolated workflows are paused so no download/network side effect can run.
with db() as c:
    c.execute("UPDATE pipeline_runs SET state='paused' WHERE engine='rust-v2' AND task_id<>%s",(ready,))
    c.execute("INSERT INTO app_settings(key,value_json) VALUES('subtitle.auto_profile',%s) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({"auto_publish":True,"auto_publish_platforms":["bilibili"],"auto_dub_p2":True}),))
    # Disable network scanners independently of the test runtime config.
    c.execute("INSERT INTO app_settings(key,value_json) VALUES('youtube.settings',%s) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({"home_scan_enabled":False}),))
worker_env={**env,"VIDEOROLL_V2_WORKERS":"1","VIDEOROLL_V2_SOURCE_SCANNER":"false","VIDEOROLL_V2_REMOTE_RENDER":"false","VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI":"false","VIDEOROLL_V2_ENABLE_SAU_PUBLISH":"false","VIDEOROLL_V2_WORKER_GATEWAY":"","VIDEOROLL_V2_YOUDUB_PYTHON":"","VIDEOROLL_V2_YOUDUB_BRIDGE":""}
log=root/"ownership-worker.log"
with log.open("w") as out:
    proc=subprocess.Popen([str(exe),"worker"],env=worker_env,stdout=out,stderr=subprocess.STDOUT)
    try:
        end=time.monotonic()+40
        while time.monotonic()<end:
            assert proc.poll() is None,log.read_text()
            with db() as c:
                state=c.execute("SELECT state FROM pipeline_runs WHERE task_id=%s AND engine='rust-v2'",(ready,)).fetchone()[0]
                if state=="succeeded":break
                assert state!="failed",log.read_text()
            time.sleep(.25)
        else:raise AssertionError(log.read_text())
        with db() as c:
            stages=dict(c.execute("SELECT stage,result_json FROM operation_inbox WHERE task_id=%s AND runtime_engine='rust-v2'",(ready,)).fetchall())
            assert set(stages)=={"render","dubbing","publish","cleanup"},stages
            assert stages["publish"]["skipped"] and stages["dubbing"]["skipped"],stages
            assert c.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(ready,)).fetchone()[0]==0
            assert c.execute("SELECT count(*) FROM outbox_events").fetchone()[0]==0
            assert c.execute("SELECT status::text FROM tasks WHERE id=%s",(ready,)).fetchone()[0]=="rendered"
        print("PASS: actual Rust worker resumes render, skips dubbing/publish despite global auto profile, retains media",flush=True)
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
