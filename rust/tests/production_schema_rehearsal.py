"""Real production DDL rehearsal: trusted schema-only dump + synthetic rows.
No production connections, account secrets, user content, or real platform calls.
"""
import hashlib,json,os,socket,subprocess,sys,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
import psycopg

uri=os.environ["DATABASE_URL"]
parsed=urlsplit(uri)
assert parsed.hostname=="127.0.0.1" and parsed.path=="/videoroll_test"
assert os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]=="isolated-schema-rehearsal-token-0123456789"
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name.startswith("v2-schema-rehearsal.")
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
manifest=root/"fixture-manifest.json"
# Explicit allowlist. Never copy whole production objects or nested secrets.
fields={
"subtitle.asr":"default_engine default_model default_language openvino_device openvino_num_beams openvino_max_new_tokens openvino_vad_enabled openvino_vad_threshold",
"subtitle.auto_profile":"asr_engine asr_language asr_model target_lang translate_enabled translate_provider bilingual burn_in soft_sub formats use_intel_gpu video_codec video_crf video_preset prefer_youtube_subtitles youtube_subtitle_mode auto_dub_p2 auto_publish auto_publish_platforms",
"subtitle.translate":"default_provider default_target_lang default_batch_size default_max_retries rag_enabled rag_top_k rag_min_score rag_embedding_provider rag_embedding_model rag_embedding_dimensions rag_embedding_device rag_embedding_timeout_seconds rag_dictionary_enabled rag_dictionary_top_k rag_auto_discover_terms rag_auto_learn_terms rag_search_enabled rag_wiki_enabled rag_agent_parallelism rag_agent_timeout_seconds",
"subtitle.task_queue":"max_concurrency",
"workflow.youtube_download_concurrency":"max_concurrent",
"youtube.settings":"compatibility_mode_enabled cookies_enabled home_scan_enabled home_scan_interval_minutes home_scan_limit home_scan_long_videos_only home_scan_min_duration_seconds parallel_download_connections parallel_download_enabled",
"storage.retention":"asset_ttl_days",
"bilibili.publish":"ai_activity_enabled upload_workers",
"publish.review.settings":"enabled",
"live.settings":"audio_bitrate_kbps fps keyframe_interval_seconds video_bitrate_kbps",
}
env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","LANG","LC_ALL")}
env.update(DATABASE_URL=uri,VIDEOROLL_V2_STORAGE_ROOT=str(root),
           VIDEOROLL_V2_INTERNAL_TOKEN=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"],
           INTERNAL_API_SECRET="isolated-schema-rehearsal-service-secret",
           VIDEOROLL_V2_STANDALONE="true",VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="true",
           VIDEOROLL_V2_SOURCE_SCANNER="false",VIDEOROLL_V2_REMOTE_RENDER="false",
           VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI="false",VIDEOROLL_V2_ENABLE_SAU_PUBLISH="false",
           VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH="false",VIDEOROLL_V2_COMPAT_WRITE="false",
           VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH="false",
           VIDEOROLL_V2_WORKERS="1")
def db():return psycopg.connect(uri)
def run(*args,ok=True):
    p=subprocess.run([str(exe),*args],env=env,text=True,capture_output=True,timeout=45)
    assert (p.returncode==0)==ok,(args,p.returncode,p.stdout,p.stderr)
    return json.loads(p.stdout[p.stdout.index("{"):]) if "{" in p.stdout else None
def task(c,status):
    t=uuid.uuid4()
    c.execute("INSERT INTO tasks(id,source_type,source_license,status,priority,retry_count) VALUES(%s,'local','own',%s,0,0)",(t,status))
    return str(t)
def setting(c,key,value):
    c.execute("INSERT INTO app_settings(key,value_json) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(key,json.dumps(value)))

if sys.argv[1]=="seed":
    raw=json.loads(Path(os.environ["VIDEOROLL_TEST_CONTROLS_SNAPSHOT"]).read_text())
    assert raw and set(raw)<=set(fields)
    for key,values in raw.items():
        assert isinstance(values,dict) and set(values)<=set(fields[key].split()),key
        for k,v in values.items():
            assert isinstance(v,(str,int,float,bool,list)) or v is None,(key,k)
            if isinstance(v,list):assert all(isinstance(x,str) for x in v)
    # Keep original controls in the isolated snapshot; disable all activity
    # until migration/preservation assertions complete. No API started yet.
    with db() as c:
        assert c.execute("SELECT to_regclass('public._sqlx_migrations')").fetchone()[0] is None
        for k,v in raw.items():setting(c,k,v)
        ids={k:task(c,s) for k,s in {"published":"published","unknown":"failed","render":"subtitle_ready","canceled":"canceled","launch_failed":"failed"}.items()}
        batch=str(uuid.uuid4());job=str(uuid.uuid4());unknown=str(uuid.uuid4());runid=str(uuid.uuid4());evt=str(uuid.uuid4())
        c.execute("INSERT INTO publish_batches(id,task_id,state,expected_targets,request_json,outcomes_json,cleanup_delivery_version,finished_at) VALUES(%s,%s,'succeeded','[]','{}','{}',1,now())",(batch,ids["published"]))
        c.execute("INSERT INTO publish_jobs(id,task_id,batch_id,platform,state,bvid,meta_json) VALUES(%s,%s,%s,'bilibili','published','fixture-known-BV','{}')",(job,ids["published"],batch))
        c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,meta_json) VALUES(%s,%s,'bilibili','unknown','{}')",(unknown,ids["unknown"]))
        c.execute("INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,external_run_id) VALUES(%s,%s,'hatchet','VideoPipelineV1','launch_failed','fixture-stopped-run')",(runid,ids["launch_failed"]))
        key=f"workflow-publish-finished:{batch}:succeeded"
        c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status) VALUES(%s,'workflow.publish.finished','publish_batch',%s,'subtitle_service.push_publish_workflow_event',%s,%s,'pending')",(evt,batch,json.dumps({"args":[batch,"succeeded",ids["published"]]}),key))
        # Python writes 'succeeded', not 'completed', and retains the
        # execution's lease timestamp after finishing. The linked job is
        # succeeded and its business lease released. Incomplete evidence
        # (no finish timestamp) must still block the cutover.
        worker=str(uuid.uuid4())
        c.execute("INSERT INTO render_workers(id,worker_key,name,platform) VALUES(%s,'fixture-worker','Fixture Worker','linux')",(worker,))
        render_execs={}
        for name,finished in (("published",True),("canceled",False)):
            render_job,execution=str(uuid.uuid4()),str(uuid.uuid4())
            c.execute("INSERT INTO render_jobs(id,task_id,status,progress,retry_count,request_json,finished_at) VALUES(%s,%s,'succeeded',100,0,'{}',now())",(render_job,ids[name]))
            c.execute("INSERT INTO render_executions(id,render_job_id,worker_id,fence_token,state,progress,lease_until,finished_at) VALUES(%s,%s,%s,%s,'succeeded',100,now()-interval '1 day',CASE WHEN %s THEN now() ELSE NULL END)",(execution,render_job,worker,'fixture-'+execution,finished))
            render_execs[name]=execution
        # Real but tiny synthetic media, preserving original asset UUID/key.
        folder=root/"legacy-fixture";folder.mkdir()
        video=folder/"source.mp4"
        subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i","color=black:s=64x64:d=1","-c:v","mpeg4","-y",str(video)],check=True,timeout=20)
        sub=folder/"subtitle.srt";sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nfixture\n")
        assets=[]
        for path,kind in [(video,"video_raw"),(sub,"subtitle_srt")]:
            a=str(uuid.uuid4());k=str(path.relative_to(root));sha=hashlib.sha256(path.read_bytes()).hexdigest()
            c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s,%s,%s,%s)",(a,ids["render"],kind,k,sha,path.stat().st_size))
            assets.append([a,k,sha])
    manifest.write_text(json.dumps({"ids":ids,"job":job,"unknown":unknown,"run":runid,"event":evt,"key":key,"assets":assets,"config":raw,"render_execs":render_execs}))
    print("PASS: seeded original DDL before SQLx, synthetic historical rows and",len(raw),"safe production settings sections",flush=True)
    sys.exit(0)

assert sys.argv[1]=="verify"
m=json.loads(manifest.read_text());ids=m["ids"]
with db() as c:
    assert c.execute("SELECT count(*) FROM _sqlx_migrations WHERE success").fetchone()[0]==39
    assert c.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'vr2_%'").fetchone()[0]==0
    assert c.execute("SELECT count(*) FROM tasks WHERE runtime_owner='python'").fetchone()[0]==5
    for k,v in m["config"].items():
        assert c.execute("SELECT value_json FROM app_settings WHERE key=%s",(k,)).fetchone()[0]==v,k
    assert c.execute("SELECT bvid,state::text FROM publish_jobs WHERE id=%s",(m["job"],)).fetchone()==("fixture-known-BV","published")
    for a,k,h in m["assets"]:
        assert c.execute("SELECT storage_key,sha256 FROM assets WHERE id=%s",(a,)).fetchone()==(k,h)
    assert c.execute("SELECT state,external_run_id FROM pipeline_runs WHERE id=%s",(m["run"],)).fetchone()==("launch_failed","fixture-stopped-run")
    assert c.execute("SELECT status,task_name FROM outbox_events WHERE id=%s",(m["event"],)).fetchone()==("pending","subtitle_service.push_publish_workflow_event")
print("PASS: 39 migrations preserve original config, task/asset IDs, publication receipt, historical run and pending outbox; no physical vr2 tables",flush=True)
pre=run("standalone-preflight",ok=False)
assert pre["historical_blockers"]["legacy_tasks"]==5
assert pre["historical_blockers"]["uncertain_publications"]==1
assert pre["historical_blockers"]["legacy_workflows"]==1
assert pre["historical_blockers"]["legacy_render_executions"]==1
run("standalone-preflight","--startup",ok=False)
audit=run("task-ownership-audit","--limit","20")
assert audit["python_owned_total"]==5
blockers={r["task_id"]:r["blockers"] for r in audit["items"]}
assert "publication_unsettled" in blockers[ids["unknown"]]
assert "active_workflow" in blockers[ids["launch_failed"]]
assert "render_execution_unsettled" not in blockers[ids["published"]]
assert "render_execution_unsettled" in blockers[ids["canceled"]]
def adopt(name,*args,ok=True):
    return run("task-ownership-adopt","--task",ids[name],"--quiesced",*args,ok=ok)
adopt("unknown",ok=False)
adopt("launch_failed",ok=False)
adopt("launch_failed","--stopped-run",str(uuid.uuid4()),ok=False)
adopt("published","--resume",ok=False)
a=adopt("published")
assert a["workflow_id"] is None
assert adopt("published")==a
adopt("canceled",ok=False)
with db() as c:
    assert c.execute("SELECT runtime_owner FROM tasks WHERE id=%s",(ids["canceled"],)).fetchone()[0]=="python"
    # Explicit fixture-only verified completion; never do this by guessing
    # an actual historical execution's finish timestamp.
    c.execute("UPDATE render_executions SET finished_at=now() WHERE id=%s",(m["render_execs"]["canceled"],))
adopt("canceled")
with db() as c:
    assert c.execute("SELECT state,finished_at IS NOT NULL,lease_until IS NOT NULL FROM render_executions WHERE id=%s",(m["render_execs"]["published"],)).fetchone()==("succeeded",True,True)
print("PASS: original succeeded render with finish evidence is terminal; incomplete success remains blocked",flush=True)
r=adopt("render","--resume")
assert r["resumed_stage"]=="render" and r["automatic_publish"] is False
assert set(x["asset_id"] for x in r["verified_assets"])==set(a[0] for a in m["assets"])
assert adopt("render","--resume")==r
adopt("launch_failed","--stopped-run",m["run"])
with db() as c:
    assert c.execute("SELECT state,external_run_id FROM pipeline_runs WHERE id=%s",(m["run"],)).fetchone()==("superseded","fixture-stopped-run")
    assert c.execute("SELECT state::text FROM publish_jobs WHERE id=%s",(m["unknown"],)).fetchone()[0]=="unknown"
    assert c.execute("SELECT runtime_owner FROM tasks WHERE id=%s",(ids["unknown"],)).fetchone()[0]=="python"
run("standalone-preflight",ok=False)
print("PASS: actual production DDL ownership guards, idempotent adoption, verified media resume, unknown publication quarantine, explicit stopped-run confirmation",flush=True)
report=run("outbox-adopt","--quiesced")
assert report["adopted_batch_events"]==1,report
run("publish-batch-tick")
with db() as c:
    assert c.execute("SELECT status FROM outbox_events WHERE id=%s",(m["event"],)).fetchone()[0]=="native_completed"
    assert c.execute("SELECT request_json->>'outbox_event_id',status FROM operation_inbox WHERE operation_key=%s",(m["key"],)).fetchone()==(m["event"],"completed")
    assert c.execute("SELECT count(*) FROM publish_jobs").fetchone()[0]==2
print("PASS: original pending batch event adopted and acknowledged using original NOT NULL request_json, no duplicate publication",flush=True)
# Explicit SYNTHETIC operator reconciliation. The real unknown records are
# untouched and must not be marked failed without external evidence.
with db() as c:
    c.execute("UPDATE publish_jobs SET state='published',bvid='fixture-verified-external-BV' WHERE id=%s AND state='unknown'",(m["unknown"],))
    c.execute("UPDATE tasks SET status='published' WHERE id=%s",(ids["unknown"],))
adopt("unknown")
assert run("standalone-preflight")["ready"] is True
# Safety overrides only after verifying the copied controls survived migration.
# Do not run a real scanner or use configured platform/model endpoints.
with db() as c:
    for key,overrides in {
        "youtube.settings":{"home_scan_enabled":False,"cookies_enabled":False},
        "subtitle.auto_profile":{"auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"translate_enabled":False},
        "subtitle.translate":{"rag_enabled":False,"rag_search_enabled":False,"rag_wiki_enabled":False},
        "publish.review.settings":{"enabled":False},
    }.items():
        c.execute("UPDATE app_settings SET value_json=value_json||%s::jsonb WHERE key=%s",(json.dumps(overrides),key))
with socket.socket() as sock:sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
env["VIDEOROLL_V2_BIND"]=f"127.0.0.1:{port}"
def call(path,body=None):
    req=Request(f"http://127.0.0.1:{port}"+path,data=None if body is None else json.dumps(body).encode(),
        headers={"x-videoroll-v2-internal-token":env["VIDEOROLL_V2_INTERNAL_TOKEN"],"Content-Type":"application/json"})
    try:r=urlopen(req,timeout=15)
    except HTTPError as e:r=e
    with r:return r.status,json.loads(r.read())
log=root/"api.log"
with log.open("w") as f:
    proc=subprocess.Popen([str(exe),"api"],env=env,stdout=f,stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            assert proc.poll() is None,log.read_text()
            try:
                if call("/ready")[0]==200:break
            except URLError:pass
            time.sleep(.25)
        else:raise AssertionError("isolated API not ready")
        # Verify settings projected from the production controls, not defaults.
        for key,path,chosen in [
            ("subtitle.asr","/api/v1/subtitle/asr/settings",["default_engine","default_model","openvino_device"]),
            ("subtitle.translate","/api/v1/subtitle/translate/settings",["rag_embedding_provider","rag_embedding_model","rag_embedding_dimensions"]),
            ("youtube.settings","/api/v1/settings/youtube",["parallel_download_enabled","parallel_download_connections"]),
        ]:
            code,value=call(path);assert code==200,(path,code,value)
            for k in chosen:
                if k in m["config"].get(key,{}):assert value[k]==m["config"][key][k],(key,k,value[k],m["config"][key][k])
        for kind in ["local","youtube"]:
            code,body=call("/api/v1/tasks",{"source_type":kind,"source_license":"own","source_url":None if kind=="local" else "https://www.youtube.com/watch?v=Rehearsal01"})
            assert code==201,(kind,code,body)
            with db() as c:
                assert c.execute("SELECT retry_count,runtime_owner FROM tasks WHERE id=%s",(body["id"],)).fetchone()==(0,"rust")
        for version,src in [("v1","UC12345678901234567890AB"),("v2","UC12345678901234567890CD")]:
            code,body=call(f"/api/{version}/youtube/sources",{"source_type":"channel","source_id":src,"source_url":"https://www.youtube.com/channel/"+src,"license":"authorized","enabled":False,"auto_process":False})
            assert code in (200,201),(version,code,body)
            with db() as c:
                assert c.execute("SELECT last_scan_discovered_count,last_scan_created_count,last_scan_started_pipeline_count,last_scan_skipped_duplicates FROM youtube_sources WHERE id=%s",(body["id"],)).fetchone()==(0,0,0,0)
        print("PASS: real DDL standalone API, production control readback, local/YouTube task creation and both source CRUD entry points",flush=True)
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
print("PASS: strict admission only after synthetic reconciliation; no actual platform calls",flush=True)
