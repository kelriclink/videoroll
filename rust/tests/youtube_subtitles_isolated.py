"""Original YouTube caption selection through the actual Rust subtitle worker.
All media/yt-dlp outputs and model requests are synthetic; no external network.
"""
import hashlib,json,os,subprocess,threading,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import psycopg
url=os.environ["DATABASE_URL"]
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert urlsplit(url).hostname=="127.0.0.1" and urlsplit(url).path=="/videoroll_test"
assert root.name.startswith("v2-youtube-subtitles.")
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
assert token=="isolated-youtube-subtitles-token-0123456789"
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
def db():return psycopg.connect(url)
def setting(key,value):
    with db() as c:c.execute("INSERT INTO app_settings(key,value_json,version) VALUES(%s,%s,1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(key,json.dumps(value)))
setting("subtitle.auto_profile",{"translate_enabled":True,"translate_provider":"mock","target_lang":"zh","auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False,"youtube_subtitle_mode":"target"})
setting("youtube.settings",{"home_scan_enabled":False})
setting("subtitle.translate",{"rag_enabled":False})
mock=root/"mock-ytdlp"
mock.write_text(r'''#!/usr/bin/env python3
import sys,json,os
from pathlib import Path
args=sys.argv[1:]
assert "--ignore-config" in args and "--no-playlist" in args and "--skip-download" in args
assert "skip=translated_subs" in args[args.index("--extractor-args")+1]
assert args[-1].startswith("https://www.youtube.com/watch?v=")
vid=args[-1].split("=")[-1]
with open(os.environ["VR_CAPTION_CALLS"],"a") as f:f.write(json.dumps([vid,args])+"\n")
if "--dump-single-json" in args:
    if vid=="PROBEFAIL01":sys.exit(1)
    info={"id":vid,"original_language":"en","subtitles":{"zh-Hans":[{"ext":"vtt"}]},"automatic_captions":{"zh":[{"ext":"vtt"}],"en-orig":[{"ext":"vtt"}],"en":[{"ext":"vtt"}]}}
    if vid=="AUTOTARGET1":info["subtitles"]={}
    if vid=="NOTRACK0001":info["subtitles"]={};info["automatic_captions"]={}
    if vid=="WRONGID0001":info["id"]="XXXXXXXXXXX"
    print(json.dumps(info));sys.exit(0)
if vid=="DOWNLOADF01":sys.exit(1)
lang=args[args.index("--sub-langs")+1]
assert lang.startswith("^") and lang.endswith("$")
lang=lang[1:-1]
if vid.startswith("AUTOSOURCE"):
    assert lang=="en" and "--write-auto-subs" in args and "--no-write-subs" in args
    text="original English cue"
elif vid=="AUTOTARGET1":
    assert lang=="zh" and "--write-auto-subs" in args
    text="自动中文字幕"
else:
    assert lang=="zh-Hans" and "--write-subs" in args and "--no-write-auto-subs" in args
    text="人工中文字幕"
if vid=="EMPTYTRACK1":Path(f"track.{lang}.vtt").write_text("WEBVTT\n\n");sys.exit(0)
if vid=="OUTOFRANGE1":start,end="00:00:50.000","00:00:51.000"
else:start,end="00:00:00.100","00:00:00.900"
Path(f"track.{lang}.vtt").write_text(f"WEBVTT\n\n{start} --> {end}\n{text}\n\n")
''')
mock.chmod(0o700)
calls=root/"calls.jsonl"
calls.touch()
asr=[]
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        data=self.rfile.read(int(self.headers["Content-Length"]))
        assert self.path=="/v1/audio/transcriptions" and b"fLaC" in data
        asr.append(data)
        raw=json.dumps({"segments":[{"start":0.2,"end":0.8,"text":"ASR fallback"}]}).encode()
        self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
    def log_message(self,*a):pass
server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH","DATABASE_URL","VIDEOROLL_V2_STORAGE_ROOT","VIDEOROLL_V2_INTERNAL_TOKEN","INTERNAL_API_SECRET")}
env.update(VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="true",VIDEOROLL_V2_SOURCE_SCANNER="false",VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI="false",VIDEOROLL_V2_ENABLE_SAU_PUBLISH="false",VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH="false",VIDEOROLL_V2_COMPAT_WRITE="false",VIDEOROLL_V2_REMOTE_RENDER="false",VIDEOROLL_V2_WORKERS="1",VIDEOROLL_V2_YT_DLP=str(mock),VR_CAPTION_CALLS=str(calls),VIDEOROLL_V2_BIND="127.0.0.1:18080")
def api(path,body):
    req=Request("http://127.0.0.1:18080/api/v1"+path,method="POST",data=json.dumps(body).encode(),headers={"x-videoroll-v2-internal-token":token,"Content-Type":"application/json"})
    try:
        with urlopen(req,timeout=10) as r:return r.status,json.load(r)
    except HTTPError as e:return e.code,json.load(e)
def wait(fn,secs=45):
    end=time.monotonic()+secs
    while time.monotonic()<end:
        result=fn()
        if result:return result
        time.sleep(.2)
    raise AssertionError("caption test timeout")
def seed(vid,mode,translate=True,local=False,via="standalone",audio=True,extra=None,origin=None,prior=False):
    t=uuid.uuid4();key=f"v2/raw/{t}/raw.mp4";video=root/key;video.parent.mkdir(parents=True)
    args=["ffmpeg","-nostdin","-v","error","-f","lavfi","-i","color=black:s=64x64:r=5:d=2"]
    if audio:args+=["-f","lavfi","-i","sine=frequency=440:sample_rate=16000:duration=2","-shortest","-c:a","aac"]
    subprocess.run(args+["-c:v","mpeg4","-y",str(video)],check=True,timeout=30)
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(%s,%s,%s,'authorized','downloaded','rust')",(t,"local" if local else "youtube",None if local else "https://youtu.be/"+vid))
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw',%s,%s,%s)",(uuid.uuid4(),t,key,hashlib.sha256(video.read_bytes()).hexdigest(),video.stat().st_size))
    if origin or prior:
        with db() as c:
            if origin:c.execute("UPDATE tasks SET origin_kind=%s WHERE id=%s",(origin,t))
            if prior:
                c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(%s,%s,1,'succeeded','subtitle',%s)",(uuid.uuid4(),t,json.dumps({"subtitle":{"youtube_subtitle_mode":"off","translate_enabled":False,"translate_provider":"noop"},"render":{"burn_in":True},"auto_publish":True,"auto_publish_platforms":["bilibili"],"auto_dub_p2":True})))
    if via=="pipeline":
        w,j=uuid.uuid4(),uuid.uuid4()
        with db() as c:
            c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(%s,%s,1,'running','subtitle',%s)",(w,t,json.dumps({"auto_publish":False,"auto_dub_p2":False,"subtitle":{"translate_enabled":translate,"translate_provider":"mock"}})))
            c.execute("INSERT INTO operation_inbox(id,workflow_id,task_id,stage,generation,operation_key,request_json,max_attempts,runtime_engine) VALUES(%s,%s,%s,'subtitle',1,%s,'{}',3,'rust-v2')",(j,w,t,f"vr2:{w}:subtitle:1"))
        return t
    if via=="manual":
        payload={"translate_enabled":translate,"translate_provider":"mock","target_lang":"zh","burn_in":False,"prefer_youtube_subtitles":mode!="off","youtube_subtitle_mode":mode}
        status,value=api(f"/tasks/{t}/actions/subtitle",payload)
    else:
        payload={"task_id":str(t),"input":{"type":"storage","key":key},"prefer_youtube_subtitles":mode!="off","youtube_subtitle_mode":mode,"translate":{"enabled":translate,"provider":"mock","target_lang":"zh"},"output":{"render":{"burn_in":False}}}
        if extra:
            payload.update(extra)
            payload["output_prefix"]=f"sub/{t}/"
        status,value=api("/subtitle/jobs",payload)
        if extra:
            assert status==200,(status,value)
            code,same=api("/subtitle/jobs",payload)
            assert code==200 and same==value,(code,same)
            code,bad=api("/subtitle/jobs",{**payload,"output_prefix":"sub/another-task/"})
            assert code in (400,422),(code,bad)
            with db() as c:
                cfg=c.execute("SELECT configuration FROM vr2_workflows WHERE task_id=%s ORDER BY generation DESC LIMIT 1",(t,)).fetchone()[0]
            expected=extra.get("runtime_profile",origin in ("auto_youtube","youtube_home_scan","youtube_task_restart"))
            assert cfg["standalone_subtitle_runtime_profile"] is expected,cfg
            if expected:
                assert cfg["subtitle"]=={} and cfg["render"]=={} and "auto_publish" not in cfg and "auto_publish_platforms" not in cfg and "auto_dub_p2" not in cfg,cfg
            else:
                assert cfg["subtitle"]["youtube_subtitle_mode"]==mode and cfg["auto_publish"] is False and cfg["auto_dub_p2"] is False,cfg

    assert status in (200,201,202),(status,value)
    return t
def result(t):
    with db() as c:
        row=c.execute("SELECT status,result_json,last_error FROM operation_inbox WHERE task_id=%s AND stage='subtitle' AND runtime_engine='rust-v2'",(t,)).fetchone()
    if row and row[0] in ("failed","unknown","canceled"):raise AssertionError(row)
    if not row or row[0]!="succeeded":return None
    return row[1]
def inspect(t,expected,reason=None,lang=None):
    value=wait(lambda:result(t))
    assert value["no_speech"] is False,value
    text=(root/value["srt_key"]).read_text()
    segments=json.loads((root/value["segments_key"]).read_text())
    assert expected in text,(value,text)
    if reason:
        assert value["youtube_subtitle"]["reason"]==reason and value["youtube_subtitle"]["language"]==lang,value
        assert segments["youtube_subtitle"]==value["youtube_subtitle"]
        assert segments["translation"]==(reason=="auto_source" and expected.startswith("[zh]"))
        if reason=="target":assert "[zh]" not in text
    else:assert value["youtube_subtitle"] is None
    return value
procs=[]
def start_worker(with_asr):
    e=dict(env)
    if with_asr:e.update(VIDEOROLL_V2_ASR_ENDPOINT=f"http://127.0.0.1:{server.server_address[1]}/v1/audio/transcriptions",VIDEOROLL_V2_ASR_MODEL="fixture-whisper")
    f=(root/("worker-asr.log" if with_asr else "worker-captions.log")).open("w")
    p=subprocess.Popen([str(exe),"worker"],env=e,stdout=f,stderr=subprocess.STDOUT);procs.append((p,f));return p
def stop(p):
    p.terminate()
    try:p.wait(timeout=8)
    except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
try:
    f=(root/"api.log").open("w");p=subprocess.Popen([str(exe),"api"],env=env,stdout=f,stderr=subprocess.STDOUT);procs.append((p,f))
    def ready():
        try:
            with urlopen("http://127.0.0.1:18080/health",timeout=1) as r:return r.status==200
        except Exception:return False
    wait(ready,20)
    a=seed("MANUALCAP01","target",via="manual",audio=False)
    b=seed("AUTOTARGET1","target")
    c=seed("AUTOSOURCE1","auto_source")
    d=seed("PIPELINE001","target",via="pipeline")
    worker=start_worker(False)
    inspect(a,"人工中文字幕","target","zh-Hans")
    inspect(b,"自动中文字幕","target","zh")
    inspect(c,"[zh] original English cue","auto_source","en")
    inspect(d,"人工中文字幕","target","zh-Hans")
    assert not asr
    stop(worker)
    print("PASS: manual and automatic target captions without ASR configuration, actual VTT conversion, automatic source translation and provenance",flush=True)
    # These requests intentionally conflict with the saved profile when
    # admitted. Automatic execution re-reads settings when the worker starts.
    e=seed("AUTOSOURCE2","off",translate=False,extra={"runtime_profile":True,"after_render":{"publish":False}},prior=True)
    f=seed("AUTOSOURCE3","off",translate=False,extra={"after_render":{"publish":False}},origin="youtube_home_scan")
    g=seed("MANUALCAP02","target",translate=False,extra={"runtime_profile":False},origin="auto_youtube",prior=True)
    setting("subtitle.auto_profile",{"translate_enabled":True,"translate_provider":"mock","target_lang":"zh","auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False,"youtube_subtitle_mode":"auto_source"})
    worker=start_worker(False)
    inspect(e,"[zh] original English cue","auto_source","en")
    inspect(f,"[zh] original English cue","auto_source","en")
    inspect(g,"人工中文字幕","target","zh-Hans")
    assert not asr
    stop(worker)
    setting("subtitle.auto_profile",{"translate_enabled":True,"translate_provider":"mock","target_lang":"zh","auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False,"youtube_subtitle_mode":"target"})
    print("PASS: explicit and inferred runtime profile reads current worker settings, explicit false preserves manual mode, canonical output prefix and generation isolation",flush=True)
    cases=[("NOTRACK0001","target",False),("PROBEFAIL01","target",False),("DOWNLOADF01","target",False),("EMPTYTRACK1","target",False),("OUTOFRANGE1","target",False),("WRONGID0001","target",False),("OFFMODE0001","off",False),("LOCALVID001","target",True)]
    tasks=[(vid,seed(vid,mode,translate=False,local=local)) for vid,mode,local in cases]
    worker=start_worker(True)
    for vid,t in tasks:inspect(t,"ASR fallback")
    assert len(asr)==len(tasks),len(asr)
    stop(worker)
    z=seed("OFFRUNTIME1","target",translate=False,extra={"runtime_profile":True})
    setting("subtitle.auto_profile",{"translate_enabled":True,"translate_provider":"mock","target_lang":"zh","auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False,"youtube_subtitle_mode":"off","asr_engine":"auto","asr_model":"runtime-whisper","asr_language":"en"})
    worker=start_worker(True)
    inspect(z,"[zh] ASR fallback")
    assert len(asr)==len(tasks)+1 and b"runtime-whisper" in asr[-1] and b'name="language"' in asr[-1] and b"en" in asr[-1]
    print("PASS: runtime profile resolves current ASR model and translation options at execution",flush=True)

    logged=[json.loads(line) for line in calls.read_text().splitlines()]
    ids=[x[0] for x in logged]
    assert "OFFMODE0001" not in ids and "LOCALVID001" not in ids
    for vid in ("NOTRACK0001","PROBEFAIL01","WRONGID0001"):assert ids.count(vid)==1,(vid,ids)
    for vid in ("DOWNLOADF01","EMPTYTRACK1","OUTOFRANGE1"):assert ids.count(vid)==2,(vid,ids)
    print("PASS: no-track, probe/download failure, empty/out-of-range/wrong-video captions fallback to ASR; off and local skip yt-dlp",flush=True)
finally:
    for p,f in reversed(procs):
        if p.poll() is None:stop(p)
        f.close()
    server.shutdown()
