#!/usr/bin/env python3
"""Focused HTTP tests for the remaining native subtitle-service V1 adapters."""
import json, os, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import psycopg

dburl=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in dburl and dburl.endswith("/videoroll_test")
base="http://127.0.0.1:18080/api/v1"
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
headers={"x-videoroll-v2-internal-token":token,"content-type":"application/json"}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        assert self.path=="/v1/embeddings",self.path
        n=int(self.headers.get("content-length","0"))
        body=json.loads(self.rfile.read(n))
        assert body["model"]=="mock-embed-native",body
        assert body["dimensions"]==3,body
        payload=json.dumps({"data":[{"embedding":[0.25,0.5,0.75]}]}).encode()
        self.send_response(200);self.send_header("content-type","application/json")
        self.send_header("content-length",str(len(payload)));self.end_headers();self.wfile.write(payload)

server=ThreadingHTTPServer(("127.0.0.1",18111),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()

def call(method,path,payload=None):
    data=None if payload is None else json.dumps(payload).encode()
    req=Request(base+path,data=data,method=method,headers=headers)
    try:
        with urlopen(req,timeout=8) as r:
            return r.status,json.loads(r.read() or b"null")
    except HTTPError as e:
        return e.code,json.loads(e.read() or b"null")

run=uuid.uuid4()
event=uuid.uuid4()
with psycopg.connect(dburl,autocommit=True) as db:
    with db.transaction():
        db.execute("""INSERT INTO translation_agent_runs(
          id,agent_type,status,term,normalized_term,domain,target_lang,query,steps,result,error)
          VALUES(%s,'rag_term_research','running','PCB','pcb','electronics','zh',
          'What is PCB?','[]'::jsonb,'{}'::jsonb,'')""",(run,))
        db.execute("""INSERT INTO translation_agent_events(id,run_id,kind,action,status,event)
          VALUES(%s,%s,'agent','lookup','ok',
          '{"action":"lookup","output":{"term":"PCB"}}'::jsonb)""",(event,run))
        db.execute("""INSERT INTO app_settings(key,value_json,version)
          VALUES('subtitle.translate',%s::jsonb,1)
          ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,version=app_settings.version+1""",
          (json.dumps({
             "rag_embedding_provider":"openai",
             "rag_embedding_model":"mock-embed-native",
             "rag_embedding_dimensions":3,
             "rag_embedding_openai":{
               "api_key_enc":"dbplain:v1:mock-key",
               "base_url":"http://127.0.0.1:18111/v1",
               "timeout_seconds":5
             }
          }),))

status,rows=call("GET","/subtitle/agents/runs?status=running&limit=10")
assert status==200 and any(x["id"]==str(run) for x in rows),(status,rows)
found=next(x for x in rows if x["id"]==str(run))
assert found["steps"] and found["steps"][0]["action"]=="lookup",found
status,detail=call("GET",f"/subtitle/agents/runs/{run}")
assert status==200 and detail["term"]=="PCB" and detail["subtitle_job_status"] is None,(status,detail)
status,missing=call("GET",f"/subtitle/agents/runs/{uuid.uuid4()}")
assert status==404,(status,missing)
print("PASS: original translation_agent_runs list/detail and event hydration")

status,emb=call("POST","/subtitle/embedding/test",{
    "text":"hello embedding","provider":"openai","model":"mock-embed-native",
    "base_url":"http://127.0.0.1:18111/v1","api_key":"mock-key","dimensions":3
})
assert status==200 and emb=={
  "provider":"openai","model":"mock-embed-native","dimensions":3,
  "expected_dimensions":3,"ok":True
},(status,emb)
status,bad=call("POST","/subtitle/embedding/test",{"text":"","dimensions":3})
assert status==400,(status,bad)
print("PASS: original embedding test contract uses native bounded HTTP client")

status,settings=call("GET","/subtitle/settings")
required={"asr_engine","whisper_model","whisper_model_dir","whisper_device",
 "whisper_compute_type","openvino_model","openvino_device","whisper_cpu_threads",
 "whisper_num_workers","external_whisper_model","groq_whisper_model",
 "cloudflare_workers_ai_model"}
assert status==200 and required<=settings.keys(),(status,settings)
print("PASS: original subtitle runtime settings response shape")

status,intel=call("GET","/subtitle/hardware/intel")
required={"checked","available","render_device","model_name","driver","pci_slot","pci_id",
 "detail","openvino_devices","openvino_gpu_available","openvino_error"}
assert status==200 and required<=intel.keys(),(status,intel)
assert intel["checked"] is True
print("PASS: original Intel hardware diagnostic response is native and source-backed")

server.shutdown()
