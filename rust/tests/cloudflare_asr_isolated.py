"""Original Cloudflare Workers AI ASR through real native Rust workers.
Only disposable PG, synthetic PCM/audio/video, and loopback HTTP.
"""
import base64,hashlib,io,json,os,socket,subprocess,threading,time,uuid,wave
from pathlib import Path
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import psycopg
url=os.environ["DATABASE_URL"]
assert urlsplit(url).hostname=="127.0.0.1" and urlsplit(url).path=="/videoroll_test"
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name.startswith("v2-cloudflare-asr.")
assert os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]=="isolated-cloudflare-asr-token-0123456789"
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
def db():return psycopg.connect(url)
def setting(key,v):
    with db() as c:c.execute("INSERT INTO app_settings(key,value_json) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(key,json.dumps(v)))
setting("subtitle.asr",{"default_engine":"cloudflare-workers-ai","default_model":"@cf/openai/whisper-large-v3-turbo","default_language":"en",
    "cloudflare_workers_ai":{"account_id":"fixture-account","model":"@cf/openai/whisper-large-v3-turbo","api_key_enc":"dbplain:v1:fixture-cf-key"}})
setting("subtitle.auto_profile",{"translate_enabled":False,"auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False,"asr_language":"en"})
setting("youtube.settings",{"home_scan_enabled":False})
lock=threading.Lock()
observed=[];active=0;peak=0;errors=[];attempts={}
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global active,peak
        try:
            assert self.path=="/client/v4/accounts/fixture-account/ai/run/@cf/openai/whisper-large-v3-turbo",self.path
            assert self.headers.get("Authorization")=="Bearer fixture-cf-key"
            data=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert data["task"]=="transcribe" and data["language"]=="en",data.keys()
            with wave.open(io.BytesIO(base64.b64decode(data["audio"],validate=True))) as w:
                assert (w.getnchannels(),w.getsampwidth(),w.getframerate())==(1,2,16000)
                duration=w.getnframes()/16000
                pcm=w.readframes(w.getnframes())
            assert 0<duration<=30.001,duration
            # Distinct signals identify distinct source chunks, even when
            # multiple workers call in an interleaved order.
            digest=hashlib.sha256(pcm).hexdigest()
            with lock:
                active+=1;peak=max(peak,active)
                attempts[digest]=attempts.get(digest,0)+1;attempt=attempts[digest]
                observed.append((duration,digest,attempt))
            try:
                time.sleep(.25)
                if duration>29 and attempt==1:
                    code=429;out={"success":False,"errors":[{"message":"fixture rate limit"}]}
                elif duration>29:
                    code=200;out={"success":True,"result":{"words":[{"start":0.2,"end":0.8,"word":"first"},{"start":1.,"end":1.5,"word":"span"}]}}
                else:
                    code=200;out={"success":True,"result":{"text":"final fallback"}}
                raw=json.dumps(out).encode()
                self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
            finally:
                with lock:active-=1
        except Exception as e:
            with lock:errors.append(repr(e))
            self.send_error(400)
    def log_message(self,*args):pass
server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH","DATABASE_URL","VIDEOROLL_V2_STORAGE_ROOT","VIDEOROLL_V2_INTERNAL_TOKEN","INTERNAL_API_SECRET")}
env.update(VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="true",VIDEOROLL_V2_STANDALONE="true",VIDEOROLL_V2_SOURCE_SCANNER="false",VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI="false",VIDEOROLL_V2_ENABLE_SAU_PUBLISH="false",
    VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH="false",VIDEOROLL_V2_COMPAT_WRITE="false",VIDEOROLL_V2_REMOTE_RENDER="false",VIDEOROLL_V2_WORKERS="1",
    VIDEOROLL_V2_ALLOW_TEST_ASR_LOOPBACK="true",VIDEOROLL_V2_TEST_CLOUDFLARE_BASE=f"http://127.0.0.1:{server.server_address[1]}",CLOUDFLARE_ASR_MAX_CONCURRENCY="1")
tasks=[]
for idx in range(2):
    t=uuid.uuid4();key=f"legacy-fixture/{t}/raw.mp4";video=root/key;video.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i","color=black:s=64x64:r=5:d=65",
        "-f","lavfi","-i",f"aevalsrc=0.1*sin(2*PI*({210+idx*50}+t)*t):s=16000:d=65",
        "-shortest","-c:v","mpeg4","-c:a","aac","-y",str(video)],check=True,timeout=30)
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,retry_count) VALUES(%s,'local','own','downloaded','python',0)",(t,))
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw',%s,%s,%s)",(uuid.uuid4(),t,key,hashlib.sha256(video.read_bytes()).hexdigest(),video.stat().st_size))
    p=subprocess.run([str(exe),"task-ownership-adopt","--task",str(t),"--quiesced","--resume"],env=env,capture_output=True,text=True,timeout=30)
    assert p.returncode==0,p.stderr
    tasks.append(t)
procs=[]
try:
    for idx in range(2):
        log=root/f"worker-{idx}.log"
        with log.open("w") as f:p=subprocess.Popen([str(exe),"worker"],env=env,stdout=f,stderr=subprocess.STDOUT)
        procs.append((p,log))
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        for p,log in procs:assert p.poll() is None,log.read_text()
        with db() as c:
            rows=c.execute("SELECT task_id,status,last_error FROM operation_inbox WHERE task_id=ANY(%s::uuid[]) AND runtime_engine='rust-v2' AND stage='subtitle'",(tasks,)).fetchall()
        assert not errors,errors
        assert all(r[1] not in ("failed","unknown","canceled") for r in rows),(rows,[l.read_text()[-2500:] for _,l in procs])
        if len(rows)==2 and all(r[1] in ("completed","succeeded") for r in rows):break
        time.sleep(.3)
    else:raise AssertionError((rows,[l.read_text()[-3000:] for _,l in procs]))
    for t in tasks:
        with db() as c:
            key=c.execute("SELECT storage_key FROM assets WHERE task_id=%s AND kind='subtitle_srt' ORDER BY created_at DESC LIMIT 1",(t,)).fetchone()[0]
            assert c.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(t,)).fetchone()[0]==0
        text=(root/key).read_text()
        assert "00:00:00,200 --> 00:00:01,500" in text and "00:00:30,200 --> 00:00:31,500" in text,text
        assert "00:01:00,000 --> 00:01:05,000" in text,text
        assert text.count("first span")==2 and text.count("final fallback")==1,text
    assert len(observed)==10 and len(attempts)==6,(observed,attempts)
    assert peak==1,peak
    with db() as c:
        # Scheduler and maintenance also use advisory locks. Check only
        # the provider's namespaced lock keys, not unrelated live locks.
        assert c.execute("""SELECT count(*) FROM pg_locks l WHERE locktype='advisory' AND granted AND objsubid=1 AND
            EXISTS (SELECT 1 FROM generate_series(0,7) s CROSS JOIN LATERAL
             (SELECT hashtextextended('videoroll:asr:cloudflare:'||s::text,0) AS k) h
             WHERE l.classid::bigint=((h.k>>32)&4294967295) AND l.objid::bigint=(h.k&4294967295))""").fetchone()[0]==0
    print("PASS: two Rust workers, 30+30+5s PCM WAV, original language/model/key, 4 HTTP 429 retries, word spans and text fallback, exact offsets, cross-process concurrency=1, SRT original assets, no publication",flush=True)
finally:
    for p,log in procs:
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
    server.shutdown();server.server_close()
