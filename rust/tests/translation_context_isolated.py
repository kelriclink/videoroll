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
parallel=os.getenv("VIDEOROLL_TEST_PARALLEL_RAG")=="true"
web_fault=os.getenv("VIDEOROLL_TEST_RAG_WEB_FAULT")=="true"
page=os.getenv("VIDEOROLL_TEST_RAG_PAGE_DENIED")=="true"
assert not page or web_fault
multistep=os.getenv("VIDEOROLL_TEST_RAG_MULTISTEP")=="true"
learning=os.getenv("VIDEOROLL_TEST_RAG_LEARNING")=="true"
assert not learning or multistep
reject_learning=os.getenv("VIDEOROLL_TEST_RAG_VERIFIER_REJECT")=="true"
assert not reject_learning or learning
verifier_calls=[]
wiki=os.getenv("VIDEOROLL_TEST_RAG_WIKI")=="true"
wiki_fault=os.getenv("VIDEOROLL_TEST_RAG_WIKI_FAULT")=="true"
assert not wiki_fault or wiki
assert not wiki or (parallel and not web_fault and not multistep)
wiki_calls=[]
assert not multistep or (parallel and not web_fault)
assert not web_fault or parallel
web_calls=[]
source=["First 10 volts.","Second 20 volts.","Rust compiler line." if parallel else "Rust line.","Fourth line.","Fifth line."]
starts=[.2,1.2,2.2,7.2,8.2]
target=["10伏特","10伏特（第二次）","第三句","第四句","第五句"]
observed=[];errors=[];asr_calls=[];repairs=[];research_calls=[];failed_once=False
child_calls={};peak=0;active=0;child_failed=False
mutex=threading.Lock();barrier=threading.Barrier(2,timeout=8)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global child_failed
        from urllib.parse import parse_qs
        try:
            if urlsplit(self.path).path=="/w/api.php":
                assert wiki and self.headers.get("Authorization") is None
                query=parse_qs(urlsplit(self.path).query)
                assert query=={"action":["query"],"generator":["search"],"gsrsearch":["Fourth line"],"gsrnamespace":["0"],"gsrlimit":["3"],"prop":["extracts"],"exintro":["1"],"explaintext":["1"],"exchars":["1200"],"exlimit":["3"],"format":["json"],"formatversion":["2"]},query
                wiki_calls.append(query["gsrsearch"][0])
                if wiki_fault and len(wiki_calls)==1:
                    child_failed=True
                    self.send_error(503,"injected wiki failure")
                    return
                return self.reply_json({"query":{"pages":[{"pageid":123,"ns":0,"index":1,"title":"Fixture encyclopedia","extract":"fixture-wikipedia-extract","fullurl":"https://example.invalid/ignored"},{"pageid":123,"ns":0,"index":2,"title":"Duplicate","extract":"duplicate"},{"pageid":456,"ns":1,"title":"Talk","extract":"ignored"}]}})
            assert web_fault
            parsed=urlsplit(self.path)
            assert parsed.path=="/search"
            query=parse_qs(parsed.query)
            assert query=={"q":["Fourth line"],"format":["json"]},query
            web_calls.append(query["q"][0])
            if len(web_calls)==1:
                child_failed=True
                self.send_error(503,"injected search failure")
                return
            self.reply_json({"results":[{"title":"Fixture","url":"https://127.0.0.1/fixture" if page else "https://example.invalid/fixture","content":"fixture-web-evidence"}]})
        except Exception as e:
            errors.append(repr(e));self.send_error(400)
    def do_POST(self):
        global failed_once,child_failed,peak,active
        try:
            body=self.rfile.read(int(self.headers["Content-Length"]))
            if self.path=="/v1/audio/transcriptions":
                assert b"whisper-fixture" in body and b"fLaC" in body
                asr_calls.append(True)
                out={"segments":[{"start":s,"end":s+.6,"text":t} for s,t in zip(starts,source)]}
            elif self.path=="/v1/chat/completions":
                assert self.headers["Authorization"]=="Bearer fixture-translation-key"
                data=json.loads(body);assert data["model"]=="fixture-translation"
                messages=data["messages"]
                if "pre-translation RAG gate" in messages[0]["content"]:
                    research_calls.append("gate")
                    result={"terms":[{"term":"Rust","category":"proper_noun","scope":"global","priority":0.9,"need_rag":True,"need_search":True},{"term":"volts","category":"unit_or_number","scope":"global","need_rag":True},{"term":"fabricated term","category":"proper_noun","scope":"global","need_rag":True}]}
                    if parallel:
                        result["terms"][0]["term"]="Rust compiler"
                        if web_fault or wiki:result["terms"][0]["need_search"]=False
                        result["terms"].append({"term":"Fourth line","category":"domain_jargon" if learning else "task_context","scope":"global" if learning else "task","priority":0.8,"need_rag":True,"need_search":True})
                    return self.reply_json({"choices":[{"message":{"content":json.dumps(result)}}]})
                if "VideoRoll independent knowledge verifier" in messages[0]["content"]:
                    assert learning
                    payload=json.loads(messages[-1]["content"])
                    assert payload["candidate"]["source"]=="Fourth line" and payload["candidate"]["target"]=="第四句",payload
                    assert len(payload["evidence"])==1 and payload["evidence"][0]["id"]==str(extra_id),payload
                    verifier_calls.append(payload)
                    decision={"supported":True,"context_consistent":True,"should_write":True,"should_auto_approve":True,"confidence":0.95,"reason":"fixture","evidence_ids":[str(extra_id)]}
                    if len(verifier_calls)==1:
                        if reject_learning:
                            decision["evidence_ids"]=[str(uuid.uuid4())]
                            return self.reply_json({"choices":[{"message":{"content":json.dumps(decision)}}]})
                        self.send_error(503,"injected verifier failure")
                        return
                    if reject_learning:
                        decision.update(supported=False,should_write=False,should_auto_approve=False,confidence=0.2,reason="unsupported translation",evidence_ids=[])
                    return self.reply_json({"choices":[{"message":{"content":json.dumps(decision)}}]})
                if "VideoRoll term specialist" in messages[0]["content"]:
                    assert parallel
                    payload=json.loads(messages[-1]["content"]);term=payload["candidate"]["term"]
                    assert term in ("Rust compiler","Fourth line"),payload
                    if web_fault and term=="Fourth line":
                        assert "fixture-web-evidence" in json.dumps(payload["web_evidence"]),payload
                    elif wiki and term=="Fourth line":
                        assert payload["web_evidence"]==[{"title":"Fixture encyclopedia","url":"https://en.wikipedia.org/?curid=123","snippet":"fixture-wikipedia-extract"}],payload
                    else:assert not payload["web_evidence"],payload
                    assert "fixture-pending" not in json.dumps(payload),payload
                    if term=="Rust compiler":
                        assert "fixture-original-compiler" in json.dumps(payload["local_evidence"]),payload
                    with mutex:
                        active+=1;peak=max(peak,active)
                        child_calls[term]=child_calls.get(term,0)+1
                        attempt=child_calls[term]
                    try:
                        if attempt==1 and not web_fault and not wiki_fault:barrier.wait()
                        time.sleep(.15)
                        if multistep and term=="Fourth line":
                            if attempt==1:
                                assert payload["additional_lookup_allowed"] and payload["additional_query"] is None,payload
                                assert "fixture-extra-context" not in json.dumps(payload),payload
                                return self.reply_json({"choices":[{"message":{"content":json.dumps({"action":"search_knowledge","query":"Fourth line context"})}}]})
                            assert not payload["additional_lookup_allowed"] and payload["additional_query"]=="Fourth line context",payload
                            assert "fixture-extra-context" in json.dumps(payload["local_evidence"]),payload
                            if attempt==2:
                                child_failed=True
                                self.send_error(503,"injected failure after extra lookup")
                                return
                        if term=="Fourth line" and attempt==1 and not web_fault and not multistep and not wiki_fault:
                            child_failed=True
                            # Fail one specialist only. The other must be committed and reused.
                            self.send_error(503,"injected specialist failure")
                            return
                        result={"target":"Rust 编译器" if term=="Rust compiler" else "第四句","note":"fixture"}
                        return self.reply_json({"choices":[{"message":{"content":json.dumps(result)}}]})
                    finally:
                        with mutex:active-=1
                if "字幕术语研究 Agent" in messages[0]["content"]:
                    if "检索动作执行结果" in messages[-1]["content"]:
                        assert "fixture-original-compiler" in messages[-1]["content"],messages
                        assert "fixture-pending" not in messages[-1]["content"],messages
                        research_calls.append("finish")
                        result={"action":"finish","summary":"Rust tooling","terms":[{"source":"Rust","target":"Rust","note":"language"}]}
                    else:
                        assert "fabricated term" not in messages[-1]["content"] and "volts" not in messages[-1]["content"],messages
                        research_calls.append("search_knowledge")
                        result={"action":"search_knowledge","query":"Rust compiler"}
                    return self.reply_json({"choices":[{"message":{"content":json.dumps(result)}}]})
                lines=json.loads(messages[-1]["content"])
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
    def reply_json(self,obj):
        raw=json.dumps(obj,ensure_ascii=False).encode();self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
    def log_message(self,*args):pass
server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
base=f"http://127.0.0.1:{server.server_address[1]}"
setting("subtitle.auto_profile",{"translate_enabled":True,"translate_provider":"openai","translate_model_provider_id":"legacy","translate_glossary":{"volts":"伏特"},"target_lang":"zh","bilingual":False,"auto_publish":False,"auto_publish_platforms":[],"auto_dub_p2":False,"burn_in":False})
setting("subtitle.translate",{"default_batch_size":2,"default_enable_summary":True,"rag_enabled":True,"rag_auto_discover_terms":True,"rag_auto_learn_terms":learning,"rag_search_enabled":web_fault,"rag_wiki_enabled":wiki,"rag_agent_parallelism":2 if parallel else 1,"rag_domain":"electronics","openai":{"base_url":base+"/v1","model":"fixture-translation","api_key_enc":"dbplain:v1:fixture-translation-key"}})
setting("subtitle.rag",{"enabled":False,"research_blocking":parallel,"page_text_enabled":page,"max_research_steps":2,"max_web_searches":2 if web_fault else 0,"web_search_endpoint":base+"/search" if web_fault else None})
setting("youtube.settings",{"home_scan_enabled":False})
env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH","DATABASE_URL","VIDEOROLL_V2_STORAGE_ROOT","VIDEOROLL_V2_INTERNAL_TOKEN","INTERNAL_API_SECRET")}
env.update(VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="true",VIDEOROLL_V2_STANDALONE="true",VIDEOROLL_V2_SOURCE_SCANNER="false",VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI="false",VIDEOROLL_V2_ENABLE_SAU_PUBLISH="false",VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH="false",VIDEOROLL_V2_COMPAT_WRITE="false",VIDEOROLL_V2_REMOTE_RENDER="false",VIDEOROLL_V2_WORKERS="1",VIDEOROLL_V2_ASR_ENDPOINT=base+"/v1/audio/transcriptions",VIDEOROLL_V2_ASR_MODEL="whisper-fixture")
env.update(VIDEOROLL_V2_ALLOW_TEST_WIKI_LOOPBACK="true",VIDEOROLL_V2_TEST_WIKI_ENDPOINT=base+"/w/api.php")
task=uuid.uuid4();other=uuid.uuid4()
namespace=uuid.UUID("35a14aca-ebce-46a0-96a1-bae6b30d83e1")
def mid(s):return uuid.uuid5(namespace,f"videoroll-translation-memory|{task}|zh|{s.casefold()}")
key=f"fixture/{task}/raw.mp4";video=root/key;video.parent.mkdir(parents=True)
subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i","color=black:s=64x64:r=5:d=10","-f","lavfi","-i","sine=frequency=440:sample_rate=16000:duration=10","-shortest","-c:v","mpeg4","-c:a","aac","-y",str(video)],check=True,timeout=30)
with db() as c:
    c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,retry_count) VALUES(%s,'local','own','downloaded','python',0)",(task,))
    c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw',%s,%s,%s)",(uuid.uuid4(),task,key,hashlib.sha256(video.read_bytes()).hexdigest(),video.stat().st_size))
    dynamic_id=uuid.uuid4()
    if learning:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_translation_terms_lang_domain_norm ON translation_knowledge_items(target_lang,domain,normalized_term) WHERE item_type='term' AND normalized_term<>''")
    for ident,translation,status in [(dynamic_id,"fixture-original-compiler","approved"),(uuid.uuid4(),"fixture-pending","pending")]:
        c.execute("INSERT INTO translation_knowledge_items(id,item_type,term,normalized_term,translation,target_lang,domain,status,confidence) VALUES(%s,'term','Rust compiler','rust compiler',%s,'zh',%s,%s,1)",(ident,translation,"medical" if learning and status=="pending" else "electronics",status))
    extra_id=uuid.uuid4()
    if multistep:
        c.execute("INSERT INTO translation_knowledge_items(id,item_type,term,normalized_term,translation,target_lang,domain,status,confidence) VALUES(%s,'term','Fourth line context','fourth line context','fixture-extra-context','zh','electronics','approved',1)",(extra_id,))
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
            if row and row[0]=="pending" and row[1] and (failed_once or child_failed):
                # Only accelerate the isolated fixture's retry deadline.
                c.execute("UPDATE operation_inbox SET available_at=now() WHERE task_id=%s AND runtime_engine='rust-v2' AND stage='subtitle' AND status='pending'",(task,))
        assert not errors,errors
        assert row and row[0] not in ("failed","unknown","canceled"),(row,log.read_text()[-3000:])
        if row[0] in ("completed","succeeded"):break
        time.sleep(.3)
    else:raise AssertionError(log.read_text()[-3000:])
    assert [len(x[0]) for x in observed]==[2,1,1,2],observed
    assert failed_once and row[2]==(3 if parallel else 2) and len(asr_calls)==1 and len(repairs)==1,(row,asr_calls,repairs)
    examples=observed[0][1]["translation_examples"]
    assert {"reviewed-same-task","approved-global"}<={e["target"] for e in examples},examples
    assert observed[1][1]["context"]["previous_source"][-1]["translation"]==target[1]
    assert observed[1][1]==observed[2][1],"retry lost checkpoint context"
    assert observed[3][1]["context"]["scene"]["scene_id"]==2
    for i in (1,2,3):
        ctx=observed[i][1]["context"]
        assert ctx["running_summary"]=="Electrical measurements.",ctx
        assert ctx["memory"]["terminology"]==[],ctx
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
    assert meta["context_memory"]["terminology"]==[]
    assert meta["context_memory"]["ambiguities"]==[{"term":"volts","resolution":"voltage unit"}]
    assert meta["context_memory"]["topic"]=="electronics"
    assert "MUST NOT LEARN REPAIR" not in json.dumps(meta)
    assert str(dynamic_id) in meta["rag_evidence_ids"],meta
    with db() as c:
        state,plan,result=c.execute("SELECT research_state,research_plan,research_result FROM pipeline_runs WHERE task_id=%s AND engine='rust-v2'",(task,)).fetchone()
        assert state=="succeeded" and plan["gate_completed"],(state,plan)
        assert c.execute("SELECT count(*) FROM translation_knowledge_items").fetchone()[0]==(4 if learning and not reject_learning else (3 if multistep else 2))
        if parallel:
            assert (web_fault or wiki_fault or peak==2) and child_calls=={"Rust compiler":1,"Fourth line":1 if (web_fault or wiki_fault) else (3 if multistep else 2)} and research_calls==["gate"],(peak,child_calls,research_calls)
            assert len(plan["gate_candidates"])==2 and plan["parallelism"]==2
            master=uuid.UUID(plan["master_agent_id"])
            assert result["master_agent_id"]==str(master) and result["failed_children"]==0
            assert not any(v["source"]=="Rust compiler" for v in result["terms"]),result
            assert "Rust compiler → Rust 编译器" not in result["context"]["prompt"],result
            print("PASS: reviewed term takes precedence over conflicting specialist recommendation; explicit glossary protected from rolling memory",flush=True)
            parent=c.execute("SELECT agent_type,status,checkpoint FROM translation_agent_runs WHERE id=%s",(master,)).fetchone()
            assert parent[:2]==("rag_master","succeeded"),parent
            assert parent[2]["model_calls"]==(3 if (web_fault or wiki_fault) else (5 if multistep else 4)) and parent[2]["model_budget"]==6,parent
            assert parent[2]["web_calls"]==parent[2]["web_budget"]==(2 if web_fault else 0),parent
            assert parent[2]["knowledge_calls"]==(3 if multistep else 2) and parent[2]["knowledge_budget"]==8,parent
            children=c.execute("SELECT term,status,checkpoint->>'attempts' FROM translation_agent_runs WHERE parent_agent_run_id=%s ORDER BY term",(master,)).fetchall()
            assert children==[("Fourth line","succeeded","2"),("Rust compiler","succeeded","1")],children
            receipts=c.execute("SELECT term,checkpoint->'initial_evidence' FROM translation_agent_runs WHERE parent_agent_run_id=%s ORDER BY term",(master,)).fetchall()
            assert all(isinstance(v,dict) for _,v in receipts),receipts
            assert c.execute("SELECT count(*) FROM translation_agent_runs WHERE parent_agent_run_id=%s AND jsonb_typeof(checkpoint->'knowledge_receipt')='array'",(master,)).fetchone()[0]==2
            assert c.execute("SELECT count(*) FROM translation_agent_events e JOIN translation_agent_runs r ON r.id=e.run_id WHERE r.parent_agent_run_id=%s AND e.action='search_knowledge' AND e.status='succeeded'",(master,)).fetchone()[0]==2
            assert parent[2]["wiki_calls"]==(2 if wiki_fault else int(wiki)) and parent[2]["wiki_budget"]==(2 if wiki else 0),parent
            assert wiki_calls==(["Fourth line"]*(2 if wiki_fault else int(wiki))),wiki_calls
            assert parent[2]["page_calls"]==int(page) and parent[2]["page_budget"]==(2 if page else 0),parent
            assert parent[2]["verification_calls"]==(2 if learning else 0) and parent[2]["verification_budget"]==(4 if learning else 0),parent
            assert len(verifier_calls)==(2 if learning else 0),verifier_calls
            if learning:
                assert verifier_calls[0]==verifier_calls[1],verifier_calls
                assert c.execute("SELECT translation,status,created_by FROM translation_knowledge_items WHERE id=%s",(dynamic_id,)).fetchone()==("fixture-original-compiler","approved","manual")
                learned=c.execute("SELECT id,translation,status,created_by,confidence,sources,last_verified_at,embedding IS NULL FROM translation_knowledge_items WHERE normalized_term='fourth line' AND domain='electronics' AND target_lang='zh'").fetchone()
                decisions={v["term"]:v for v in result["knowledge_learning"]}
                assert decisions["Rust compiler"]["reason"]=="existing_term",decisions
                child=c.execute("SELECT id,knowledge_item_id,checkpoint->'verification_receipt' FROM translation_agent_runs WHERE parent_agent_run_id=%s AND term='Fourth line'",(master,)).fetchone()
                assert child[2]["decision"]["supported"]==(not reject_learning),child
                assert c.execute("SELECT count(*) FROM translation_agent_events WHERE run_id=%s AND action='knowledge_verify' AND status='failed'",(child[0],)).fetchone()[0]==1
                assert c.execute("SELECT count(*) FROM translation_agent_events WHERE run_id=%s AND action='knowledge_verify' AND status='succeeded'",(child[0],)).fetchone()[0]==1
                if reject_learning:
                    assert learned is None and child[1] is None
                    assert decisions["Fourth line"]["reason"]=="verification_rejected",decisions
                    assert not any(v["source"]=="Fourth line" for v in result["terms"]),result
                    print("PASS: forged evidence rejected, independent negative verdict suppresses proposed mapping and knowledge write",flush=True)
                else:
                    assert learned and learned[1:5]==("第四句","pending","agent",0.0) and learned[6] is None and learned[7],learned
                    assert learned[5][0]["knowledge_item_id"]==str(extra_id) and learned[5][0]["task_id"]==str(task),learned
                    assert child[1]==learned[0] and decisions["Fourth line"]["status"]=="pending",decisions
                    assert child[2]["decision"]["should_auto_approve"] and str(learned[0]) not in meta["rag_evidence_ids"],meta
                    print("PASS: independent verifier 503 recovered, pending-only write despite model auto-approve, original term protected and verification not replayed",flush=True)
                assert c.execute("SELECT count(*) FROM translation_agent_events WHERE run_id=%s AND action='knowledge_learning'",(master,)).fetchone()[0]==2
            else:
                assert result["knowledge_learning"]==[],result
            if page:
                assert c.execute("SELECT count(*) FROM translation_agent_runs WHERE parent_agent_run_id=%s AND checkpoint->'page_receipt'='null'::jsonb",(master,)).fetchone()[0]==1
                assert c.execute("SELECT count(*) FROM translation_agent_events e JOIN translation_agent_runs r ON r.id=e.run_id WHERE r.parent_agent_run_id=%s AND e.action='page_text' AND e.status='skipped'",(master,)).fetchone()[0]==1
                assert "fixture-web-evidence" in result["context"]["prompt"],result
                print("PASS: private search-result page denied, skipped receipt preserved, original snippet retained",flush=True)
            if wiki:
                receipt=c.execute("SELECT checkpoint->'wiki_receipt' FROM translation_agent_runs WHERE parent_agent_run_id=%s AND term='Fourth line'",(master,)).fetchone()[0]
                assert len(receipt)==1 and receipt[0]["url"]=="https://en.wikipedia.org/?curid=123",receipt
                assert c.execute("SELECT count(*) FROM translation_agent_events e JOIN translation_agent_runs r ON r.id=e.run_id WHERE r.parent_agent_run_id=%s AND e.action='wiki_search' AND e.status='succeeded'",(master,)).fetchone()[0]==1
                assert c.execute("SELECT count(*) FROM translation_agent_events e JOIN translation_agent_runs r ON r.id=e.run_id WHERE r.parent_agent_run_id=%s AND e.action='wiki_search' AND e.status='failed'",(master,)).fetchone()[0]==int(wiki_fault)
                assert "fixture-wikipedia-extract" in result["context"]["prompt"],result
                print("PASS: fixed Wikipedia search/extract, page ID provenance, independent budget and receipt recovery",flush=True)
            if multistep:
                assert str(extra_id) in meta["rag_evidence_ids"],meta
                extra=c.execute("SELECT checkpoint->>'extra_query',checkpoint->'extra_receipt' FROM translation_agent_runs WHERE parent_agent_run_id=%s AND term='Fourth line'",(master,)).fetchone()
                assert extra[0]=="Fourth line context" and any(h["id"]==str(extra_id) for h in extra[1]),extra
                assert c.execute("SELECT count(*) FROM translation_agent_events e JOIN translation_agent_runs r ON r.id=e.run_id WHERE r.parent_agent_run_id=%s AND e.action='extra_knowledge' AND e.status='succeeded'",(master,)).fetchone()[0]==1
                print("PASS: model-selected extra lookup, durable intent and receipt, final model 503 recovery without replaying lookup",flush=True)
            if web_fault:
                assert web_calls==["Fourth line","Fourth line"],web_calls
                assert c.execute("SELECT count(*) FROM translation_agent_events e JOIN translation_agent_runs r ON r.id=e.run_id WHERE r.parent_agent_run_id=%s AND e.action='search_web' AND e.status='failed'",(master,)).fetchone()[0]==1
                assert c.execute("SELECT count(*) FROM translation_agent_runs WHERE parent_agent_run_id=%s AND jsonb_typeof(checkpoint->'web_receipt')='array'",(master,)).fetchone()[0]==1
                print("PASS: web 503 retried independently, knowledge receipts reused, model budget not consumed by failed search",flush=True)
            assert c.execute("SELECT count(*) FROM translation_agent_events e JOIN translation_agent_runs r ON r.id=e.run_id WHERE r.parent_agent_run_id=%s AND e.action='initial_retrieval' AND e.status='succeeded'",(master,)).fetchone()[0]==2
            print("PASS: initial evidence checkpoint saved once per specialist, survives subsequent model failure",flush=True)
            assert c.execute("SELECT count(*) FROM translation_agent_events WHERE run_id IN (SELECT id FROM translation_agent_runs WHERE task_id=%s) AND status='failed'",(task,)).fetchone()[0]>=1
            print("PASS: specialist recovery, persisted gate and successful child not replayed; separate model, knowledge and web budgets, original history and events",flush=True)
        else:
            assert research_calls==["gate","search_knowledge","finish"],research_calls
            assert len(plan["gate_candidates"])==1 and result["tool_steps"][0]["action"]=="search_knowledge"
            print("PASS: original discovery config activates gate; grounded candidate filter, original approved dynamic lookup, durable provenance and cached research reuse; no web or automatic learning",flush=True)

    print("PASS: native worker hard glossary+adjacent duplicate repair only affected rows, numeric warning nonblocking, prior repaired checkpoint and grounded memory restored, original IDs and approved memory protected",flush=True)
finally:
    worker.terminate()
    try:worker.wait(timeout=5)
    except subprocess.TimeoutExpired:worker.kill();worker.wait(timeout=5)
    server.shutdown();server.server_close()
