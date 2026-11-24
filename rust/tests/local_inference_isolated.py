"""Real local inference + isolated PostgreSQL; nonsecret production config only."""
import hashlib,json,os,subprocess,sys,time,uuid,socket
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
import psycopg
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
snapshot=json.loads(Path(os.environ["VIDEOROLL_TEST_INFERENCE_CONFIG"]).read_text())
allowed={"subtitle.asr":{"default_engine","default_model","default_language","openvino_device","openvino_num_beams","openvino_max_new_tokens","openvino_vad_enabled","openvino_vad_threshold"},
"subtitle.translate":{"rag_embedding_provider","rag_embedding_model","rag_embedding_device","rag_embedding_dimensions","rag_embedding_model_dir","rag_embedding_timeout_seconds"},
"subtitle.auto_profile":{"translate_enabled","translate_provider","asr_language","target_lang","bilingual","burn_in","video_codec","use_intel_gpu"}}
assert set(snapshot)<=set(allowed)
for k,v in snapshot.items():assert set(v or {})<=allowed[k]
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
root.mkdir(exist_ok=True,parents=True)
env={**os.environ,"VIDEOROLL_V2_NATIVE_OUTBOX_ONLY":"true","VIDEOROLL_V2_STANDALONE":"true",
    "VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES":"true","VIDEOROLL_V2_SOURCE_SCANNER":"false",
    "VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI":"false","VIDEOROLL_V2_ENABLE_SAU_PUBLISH":"false",
    "VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH":"false","VIDEOROLL_V2_COMPAT_WRITE":"false",
    "VIDEOROLL_V2_REMOTE_RENDER":"false","VIDEOROLL_V2_WORKERS":"1",
    "VIDEOROLL_V2_WORKER_GATEWAY":"","VIDEOROLL_V2_COMPAT_URL":"","VIDEOROLL_V2_LEGACY_ORCHESTRATOR_URL":"",
    "VIDEOROLL_V2_YOUDUB_PYTHON":"","VIDEOROLL_V2_YOUDUB_BRIDGE":""}
with socket.socket() as s:s.bind(("127.0.0.1",0));port=s.getsockname()[1]
env["VIDEOROLL_V2_BIND"]=f"127.0.0.1:{port}"
def db():return psycopg.connect(url)
def setting(k,v):
    with db() as c:c.execute("INSERT INTO app_settings(key,value_json) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(k,json.dumps(v)))
for k,v in snapshot.items():
    setting(k,v)
# This case isolates ASR and prevents real publication, translation and dubbing.
profile={**(snapshot.get("subtitle.auto_profile") or {}),"translate_enabled":False,"auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False}
setting("subtitle.auto_profile",profile)
setting("youtube.settings",{"home_scan_enabled":False})
t=uuid.uuid4()
key=f"legacy-test/{t}/raw.mp4";video=root/key;video.parent.mkdir(parents=True)
subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i","color=black:s=128x128:d=8",
    "-f","lavfi","-i","flite=text='The electronic circuit uses a resistor and a capacitor. This is a video transcription test.':voice=slt",
    "-shortest","-c:v","mpeg4","-c:a","aac","-y",str(video)],check=True,timeout=20)
data=video.read_bytes()
with db() as c:
    c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','downloaded','python')",(t,))
    c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw',%s,%s,%s)",(uuid.uuid4(),t,key,hashlib.sha256(data).hexdigest(),len(data)))
r=subprocess.run([str(exe),"task-ownership-adopt","--task",str(t),"--quiesced","--resume"],env=env,capture_output=True,text=True,timeout=30)
assert r.returncode==0,r.stderr
assert json.loads(r.stdout[r.stdout.index("{"):])["resumed_stage"]=="subtitle"
procs=[]
def start(role):
    logfile=root/(role+".log")
    with logfile.open("w") as out:p=subprocess.Popen([str(exe),role],env=env,stdout=out,stderr=subprocess.STDOUT)
    procs.append((p,logfile))
    return p,logfile
def call(path,body=None,method=None,timeout=90):
    req=Request(f"http://127.0.0.1:{port}"+path,data=json.dumps(body).encode() if body is not None else None,
        method=method,headers={"x-videoroll-v2-internal-token":env["VIDEOROLL_V2_INTERNAL_TOKEN"],"Content-Type":"application/json"})
    try:r=urlopen(req,timeout=timeout)
    except HTTPError as e:r=e
    with r:return r.status,json.loads(r.read())
try:
    api,api_log=start("api")
    for i in range(80):
        assert api.poll() is None,api_log.read_text()
        try:
            if call("/ready")[0]==200:break
        except URLError:pass
        time.sleep(.25)
    else:raise AssertionError("API not ready")
    # Actual production ASR selection is visible before worker runs.
    code,view=call("/api/v1/subtitle/asr/settings")
    assert code==200 and view["default_engine"]==snapshot["subtitle.asr"]["default_engine"],(code,view)
    assert view["default_model"]==snapshot["subtitle.asr"]["default_model"]
    assert view["openvino_device"]==snapshot["subtitle.asr"]["openvino_device"]
    worker,worker_log=start("worker")
    end=time.monotonic()+180
    while time.monotonic()<end:
        assert worker.poll() is None,worker_log.read_text()
        with db() as c:
            row=c.execute("SELECT status,result_json,last_error FROM operation_inbox WHERE task_id=%s AND runtime_engine='rust-v2' AND stage='subtitle'",(t,)).fetchone()
        assert row is not None
        if row[0]=="completed" or row[0]=="succeeded":break
        assert row[0] not in ("failed","unknown","canceled"),(row,worker_log.read_text())
        time.sleep(.5)
    else:raise AssertionError(worker_log.read_text())
    with db() as c:
        srt=c.execute("SELECT storage_key FROM assets WHERE task_id=%s AND kind='subtitle_srt' ORDER BY created_at DESC LIMIT 1",(t,)).fetchone()
        assert srt
        text=(root/srt[0]).read_text()
        assert "circuit" in text.lower() or "capacitor" in text.lower(),text
        assert c.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(t,)).fetchone()[0]==0
    print("PASS: real Rust worker, original OpenVINO GPU ASR settings, actual Whisper timestamps and original assets table SRT",flush=True)
    # Local embedding is an explicit second scenario; the production setting
    # remains recorded separately as openai / Qwen3-Embedding-8B, not replaced.
    model_dir=os.environ["SUBTITLE_RAG_EMBEDDING_MODEL_DIR"]
    local={"rag_embedding_provider":"local","rag_embedding_model":"BAAI/bge-small-zh-v1.5","rag_embedding_dimensions":512,
        "rag_embedding_model_dir":model_dir,"rag_embedding_device":"openvino:CPU","rag_embedding_timeout_seconds":90}
    setting("subtitle.translate",local)
    code,out=call("/api/v1/subtitle/embedding/test",{"text":"电子电路与视频字幕翻译"})
    assert code==200 and out["dimensions"]==512 and out["ok"],(code,out)
    code,out=call("/api/v1/subtitle/embedding/test",{"text":"测试","dimensions":511})
    assert code>=400,(code,out)
    code,out=call("/api/v1/subtitle/knowledge/items",{"item_type":"document","title":"电路测试文档","content":"电阻电容组成电子电路，测试向量知识库。"})
    assert code==200,(code,out)
    with db() as c:
        row=c.execute("SELECT vector_dims(embedding),embedding_model,embedding_text_hash FROM translation_knowledge_items WHERE title='电路测试文档'").fetchone()
        assert row and row[0]==512 and row[1]=="local:BAAI/bge-small-zh-v1.5" and len(row[2])==64,row
    code,out=call("/api/v1/subtitle/knowledge/rebuild-embeddings",{"item_type":"document","limit":1})
    assert code==200 and out.get("updated")==1,(code,out)
    print("PASS: real OpenVINO local BGE 512 dimensions, dimension rejection, PostgreSQL knowledge document and rebuild",flush=True)
finally:
    for proc,log in reversed(procs):
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
