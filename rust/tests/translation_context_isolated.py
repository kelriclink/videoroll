"""Actual Rust subtitle workflow with loopback ASR and OpenAI fixtures."""
import hashlib,json,os,subprocess,threading,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import psycopg
url=os.environ["DATABASE_URL"]
assert urlsplit(url).hostname=="127.0.0.1" and urlsplit(url).path=="/videoroll_test"
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert root.name.startswith("v2-translation-context.")
assert os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]=="isolated-translation-context-token-0123456789"
exe=Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2"
def db():return psycopg.connect(url)
def setting(k,v):
    with db() as c:c.execute("INSERT INTO app_settings(key,value_json) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(k,json.dumps(v)))
source=["First 10 volts.","Second 20 volts.","Third line.","Fourth line.","Fifth line."]
starts=[.2,1.2,2.2,7.2,8.2]
target=["10伏特","10伏特（第二次）","第三句","第四句","第五句"]
observed=[];errors=[];asr_calls=[];repairs=[];failed_once=False
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global failed_once
        try:
            body=self.rfile.read(int(self.headers["Content-Length"]))
            if self.path=="/v1/audio/transcriptions":
                assert b"whisper-fixture" in body and b"fLaC" in body
                asr_calls.append(True)
                out={"segments":[{"start":s,"end":s+.6,"text":t} for s,t in zip(starts,source)]}
            elif self.path=="/v1/chat/completions":
                assert self.headers["Authorization"]=="Bearer fixture-translation-key"
                data=json.loads(body);assert data["model"]=="fixture-translation"
                lines=json.loads(data["messages"][-1]["content"])
                if isinstance(lines,dict) and "repair_rows" in lines:
                    repairs.append(lines)
                    assert [v["i"] for v in lines["repair_rows"]]==[0,1],lines
                    assert {i["type"] for i in lines["issues"]}=={"hard_term_missing","adjacent_duplicate_translation"},lines
                    assert all(v["draft"]=="10伏" for v in lines["repair_rows"])
                    # Preserve numeric warning to prove it never causes
                    # another repair call. Only corrected rows returned.
                    result={"items":[{"i":v["i"],"text":target[v["i"]]} for v in lines["repair_rows"]],"context_update":{"topic":"MUST NOT LEARN REPAIR"}}
                    raw=json.dumps({"choices":[{"message":{"content":json.dumps(result,ensure_ascii=False)}}]}).encode()
                    self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
                    return
                context=json.loads(next(m["content"].split("instructions: ",1)[1] for m in data["messages"] if m["content"].startswith("Untrusted batch context")))
                observed.append((lines,context))
                result={"items":[{"i":v["i"],"text":target[source.index(v["source"])]} for v in lines]}
                if lines[0]["source"]==source[0]:
                    assert context["translation_constraints"]==[{"source":"volts","target":"伏特","mode":"hard","applies_to_blocks":[1,2]}],context
                    result["items"]=[{"i":v["i"],"text":"10伏"} for v in lines]
                    result["updated_summary"]="Electrical measurements."
                    result["context_update"]={"topic":"electronics","terminology":[{"source":"volts","target":"伏"},{"source":"invented capacitor","target":"电容"}],"characters":[{"name":"Unseen person","target_name":"无关人物"}],"ambiguities":[{"term":"volts","resolution":"voltage unit"}],"scene_summary":"Voltage measurements"}
                if lines[0]["source"]==source[2] and not failed_once:
                    failed_once=True
                    result={"items":[],"updated_summary":"DO NOT LEARN","context_update":{"topic":"INVALID FAILED BATCH"}}
                out={"choices":[{"message":{"content":json.dumps(result,ensure_ascii=False)}}]}
            else:raise AssertionError(self.path)
            raw=json.dumps(out).encode();self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
        except Exception as e:
            errors.append(repr(e));self.send_error(400)
    def log_message(self,*args):pass
server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
base=f"http://127.0.0.1:{server.server_address[1]}"
setting("subtitle.auto_profile",{"translate_enabled":True,"translate_provider":"openai","translate_model_provider_id":"legacy","translate_glossary":{"volts":"伏特"},"target_lang":"zh","bilingual":False,"auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False})
setting("subtitle.translate",{"default_batch_size":2,"default_enable_summary":True,"rag_enabled":False,"rag_domain":"electronics","openai":{"base_url":base+"/v1","model":"fixture-translation","api_key_enc":"dbplain:v1:fixture-translation-key"}})
setting("subtitle.rag",{"enabled":False})
setting("youtube.settings",{"home_scan_enabled":False})
env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH","DATABASE_URL","VIDEOROLL_V2_STORAGE_ROOT","VIDEOROLL_V2_INTERNAL_TOKEN","INTERNAL_API_SECRET")}
env.update(VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="true",VIDEOROLL_V2_STANDALONE="true",VIDEOROLL_V2_SOURCE_SCANNER="false",VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI="false",VIDEOROLL_V2_ENABLE_SAU_PUBLISH="false",VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH="false",VIDEOROLL_V2_COMPAT_WRITE="false",VIDEOROLL_V2_REMOTE_RENDER="false",VIDEOROLL_V2_WORKERS="1",VIDEOROLL_V2_ASR_ENDPOINT=base+"/v1/audio/transcriptions",VIDEOROLL_V2_ASR_MODEL="whisper-fixture")
task=uuid.uuid4();other=uuid.uuid4()
namespace=uuid.UUID("35a14aca-ebce-46a0-96a1-bae6b30d83e1")
def mid(s):return uuid.uuid5(namespace,f"videoroll-translation-memory|{task}|zh|{s.casefold()}")
key=f"fixture/{task}/raw.mp4";video=root/key;video.parent.mkdir(parents=True)
subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i","color=black:s=64x64:r=5:d=10","-f","lavfi","-i","sine=frequency=440:sample_rate=16000:duration=10","-shortest","-c:v","mpeg4","-c:a","aac","-y",str(video)],check=True,timeout=30)
with db() as c:
    c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,retry_count) VALUES(%s,'local','own','downloaded','python',0)",(task,))
    c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw',%s,%s,%s)",(uuid.uuid4(),task,key,hashlib.sha256(video.read_bytes()).hexdigest(),video.stat().st_size))
    for ident,t,s,translated,lang,domain,status in [
        (mid(source[0]),task,source[0],"reviewed-same-task","zh","electronics","approved"),
        (uuid.uuid4(),other,source[0],"approved-global","zh","electronics","approved"),
        (uuid.uuid4(),other,"Fourth lines.","approved-fuzzy","zh","electronics","approved"),
        (uuid.uuid4(),other,source[1],"private-other-task","zh","electronics","machine"),
        (uuid.uuid4(),other,source[1],"wrong-domain","zh","medical","approved"),
        (uuid.uuid4(),other,source[1],"wrong-language","ja","electronics","approved")]:
        c.execute("INSERT INTO translation_memory_entries(id,source_text,source_norm,target_text,target_lang,domain,task_id,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(ident,s,s.casefold(),translated,lang,domain,t,status))
p=subprocess.run([str(exe),"task-ownership-adopt","--task",str(task),"--quiesced","--resume"],env=env,capture_output=True,text=True,timeout=30)
assert p.returncode==0,p.stderr
log=root/"worker.log"
with log.open("w") as f:worker=subprocess.Popen([str(exe),"worker"],env=env,stdout=f,stderr=subprocess.STDOUT)
try:
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        assert worker.poll() is None,log.read_text()
        with db() as c:
            row=c.execute("SELECT status,last_error,attempt_count FROM operation_inbox WHERE task_id=%s AND runtime_engine='rust-v2' AND stage='subtitle'",(task,)).fetchone()
            if row and row[0]=="pending" and row[1] and failed_once:
                # Only accelerate the isolated fixture's retry deadline.
                c.execute("UPDATE operation_inbox SET available_at=now() WHERE task_id=%s AND runtime_engine='rust-v2' AND stage='subtitle' AND status='pending'",(task,))
        assert not errors,errors
        assert row and row[0] not in ("failed","unknown","canceled"),(row,log.read_text()[-3000:])
        if row[0] in ("completed","succeeded"):break
        time.sleep(.3)
    else:raise AssertionError(log.read_text()[-3000:])
    assert [len(x[0]) for x in observed]==[2,1,1,2],observed
    assert failed_once and row[2]==2 and len(asr_calls)==1 and len(repairs)==1,(row,asr_calls,repairs)
    examples=observed[0][1]["translation_examples"]
    assert {"reviewed-same-task","approved-global"}<={e["target"] for e in examples},examples
    assert observed[1][1]["context"]["previous_source"][-1]["translation"]==target[1]
    assert observed[1][1]==observed[2][1],"retry lost checkpoint context"
    assert observed[3][1]["context"]["scene"]["scene_id"]==2
    for i in (1,2,3):
        ctx=observed[i][1]["context"]
        assert ctx["running_summary"]=="Electrical measurements.",ctx
        assert ctx["memory"]["terminology"]==[{"source":"volts","target":"伏","meaning":""}],ctx
        assert ctx["memory"]["characters"]==[],ctx
    assert any(e["target"]=="approved-fuzzy" and 0.78<=e["similarity"]<1 and e["scope"]=="approved_global" for e in observed[3][1]["translation_examples"]),observed[3]
    assert "INVALID FAILED BATCH" not in json.dumps(observed) and "DO NOT LEARN" not in json.dumps(observed)
    assert all(x not in json.dumps(observed) for x in ("private-other-task","wrong-domain","wrong-language"))
    with db() as c:
        assert c.execute("SELECT target_text,status FROM translation_memory_entries WHERE id=%s",(mid(source[0]),)).fetchone()==("reviewed-same-task","approved")
        for s,t in zip(source[1:],target[1:]):
            assert c.execute("SELECT target_text,task_id,status FROM translation_memory_entries WHERE id=%s",(mid(s),)).fetchone()==(t,task,"machine")
        meta_key=c.execute("SELECT storage_key FROM assets WHERE task_id=%s AND kind='segments_json' ORDER BY created_at DESC LIMIT 1",(task,)).fetchone()[0]
        assert c.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(task,)).fetchone()[0]==0
    meta=json.loads((root/meta_key).read_text())
    assert meta["translation_scenes"]["scenes"]==[[0,3],[3,5]],meta
    assert meta["translation_scenes"]["batches"]==[[0,2],[2,3],[3,5]],meta
    assert {i["type"] for i in meta["translation_quality_issues"]}=={"number_mismatch"},meta
    assert meta["running_summary"]=="Electrical measurements."
    assert meta["context_memory"]["terminology"]==[{"source":"volts","target":"伏","meaning":""}]
    assert meta["context_memory"]["ambiguities"]==[{"term":"volts","resolution":"voltage unit"}]
    assert meta["context_memory"]["topic"]=="electronics"
    assert "MUST NOT LEARN REPAIR" not in json.dumps(meta)
    print("PASS: native worker hard glossary+adjacent duplicate repair only affected rows, numeric warning nonblocking, prior repaired checkpoint and grounded memory restored, original IDs and approved memory protected",flush=True)
finally:
    worker.terminate()
    try:worker.wait(timeout=5)
    except subprocess.TimeoutExpired:worker.kill();worker.wait(timeout=5)
    server.shutdown();server.server_close()
