#!/usr/bin/env python3
"""Python-backed schema and business tests for five Rust V1 operations APIs.
Disposable local Postgres only; never reads/writes production business data.
"""
import json,os,subprocess
from urllib.request import Request,urlopen
from urllib.error import HTTPError
BASE="http://127.0.0.1:18080/api/v1"
TOKEN=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
CONTAINER=os.environ["container_name"]
HEADERS={"x-videoroll-v2-internal-token":TOKEN,"content-type":"application/json"}
def req(method,path,body=None,auth=True):
    assert path.startswith("/") and "://" not in path
    headers=HEADERS if auth else {"content-type":"application/json"}
    q=Request(BASE+path,headers=headers,method=method,
              data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(q,timeout=10) as r:
            return r.status,json.loads(r.read(1024*1024))
    except HTTPError as e:
        return e.code,json.loads(e.read())
def pg(sql,query=False):
    cmd=["docker","exec","-i",CONTAINER,"psql","-At",
        "-v","ON_ERROR_STOP=1","-U","postgres","-d","videoroll_test"]
    if query:
        cmd.extend(["-c",sql])
        return subprocess.check_output(cmd,text=True).strip()
    subprocess.run(cmd,input=sql,text=True,check=True,stdout=subprocess.DEVNULL)
def must(code,obj,expected):
    assert code==expected,(code,expected,obj)
    return obj

# Inspect real model requests made earlier in this exact isolated smoke run.
# Stored prompts/responses are NOT collected by Rust telemetry.
status,actual=req("GET","/operations/ai-usage?hours=24&recent_limit=200")
assert status==200,(status,actual)
events=[row for row in actual["recent"] if row["operation"] in ("rag_agent","publish_review")]
assert any(row["operation"]=="rag_agent" for row in events),events
assert any(row["operation"]=="publish_review" for row in events),events
assert all(row["input_tokens"]==120 and row["output_tokens"]==45 and
           row["estimated_cost_usd"]==0.00021 and row["task_id"] for row in events),events
assert all(row["status_code"]==200 and row["success"] for row in events)
operations={row["operation"] for row in actual["recent"]}
assert {"asr_whisper","index_translate","rag_agent","publish_review"}<=operations,operations
print("PASS: native Whisper ASR and Index-Translate model requests also generate real ledger events")
print("PASS: actual RAG Agent/review HTTP attempts recorded in AI ledger, priced from model usage, associated with owning tasks")
# Each synthetic seed below tests SQL aggregation against a known fixture.
# Clear only the disposable test ledger, never the production database.
pg("DELETE FROM ai_usage_events")

# Only this disposable fixture database is reset. Earlier smoke cases
# deliberately enabled published cleanup, so clear that local override to
# test the original Python fallback independently.
pg("DELETE FROM app_settings WHERE key IN ('storage.retention','ai.usage.pricing')")

# Python/Rust share the canonical original configuration rows.
pg("""INSERT INTO app_settings(key,value_json) VALUES
 ('storage.retention','{"asset_ttl_days":42}'::jsonb),
 ('ai.usage.pricing','{"models":{"legacy-model":{"input_per_million_usd":1.5,"output_per_million_usd":3.0}}}'::jsonb)
 ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json""")
assert req("GET","/settings/storage",auth=False)[0]==401
assert req("GET","/operations/ai-usage",auth=False)[0]==401
assert req("GET","/operations/ai-usage/pricing",auth=False)[0]==401
assert must(*req("GET","/settings/storage"),200)=={"asset_ttl_days":42}
assert must(*req("GET","/operations/ai-usage/pricing"),200)["models"]["legacy-model"]["input_per_million_usd"]==1.5
assert must(*req("PUT","/settings/storage",{"asset_ttl_days":14}),200)=={"asset_ttl_days":14}
assert must(*req("PUT","/settings/storage",{"asset_ttl_days":None}),200)=={"asset_ttl_days":14}
assert must(*req("PUT","/settings/storage",{"asset_ttl_days":99999}),200)=={"asset_ttl_days":3650}
assert must(*req("PUT","/settings/storage",{"asset_ttl_days":-12}),200)=={"asset_ttl_days":0}
assert req("PUT","/settings/storage",{"asset_ttl_days":"oops"})[0]==400
assert req("GET","/settings/storage")[1]=={"asset_ttl_days":0}
assert pg("SELECT value_json->>'asset_ttl_days' FROM app_settings WHERE key='storage.retention'",True)=="0"

prices={"models":{
    "openai:qwen":{"input_per_million_usd":2.0,"output_per_million_usd":"4.0"},
    "fallback":{"input_per_million_usd":1.0,"output_per_million_usd":1.0}}}
status,result=req("PUT","/operations/ai-usage/pricing",prices)
assert status==200 and result["models"]["openai:qwen"]=={
    "input_per_million_usd":2,"output_per_million_usd":4},(status,result)
assert req("GET","/operations/ai-usage/pricing")[1]==result
assert req("PUT","/operations/ai-usage/pricing",{"models":{"bad":{"input_per_million_usd":-1}}})[0]==400
assert req("PUT","/operations/ai-usage/pricing",{"models":{"bad":"text"}})[0]==400
assert req("PUT","/operations/ai-usage/pricing",{"models":"incorrect"})[0]==400
assert req("GET","/operations/ai-usage/pricing")[1]==result
assert pg("SELECT value_json->'models'?'legacy-model' FROM app_settings WHERE key='ai.usage.pricing'",True)=="f"

# Cost is stored in integer micro-USD exactly as the Python event model.
pg("""INSERT INTO ai_usage_events
 (id,provider,model,operation,success,status_code,input_tokens,output_tokens,total_tokens,
  latency_ms,estimated_cost_microusd,error_type,error_message,created_at)
 VALUES
 ('00000000-0000-0000-0000-000000001101','openai','qwen','translation',TRUE,200,1000,500,1500,50,NULL,NULL,NULL,now()),
 ('00000000-0000-0000-0000-000000001102','openai','qwen','translation',FALSE,429,10,20,30,100,7,'rate_limit','mock failure',now()),
 ('00000000-0000-0000-0000-000000001103','other','unpriced','asr',TRUE,200,50,25,75,20,NULL,NULL,NULL,now()),
 ('00000000-0000-0000-0000-000000001104','openai','qwen','translation',TRUE,200,999,999,1998,40,NULL,NULL,NULL,now()-interval '2 days')
""")
code,usage=req("GET","/operations/ai-usage?hours=24&recent_limit=2")
assert code==200,(code,usage)
assert set(usage)=={"hours","requests","successes","failures","input_tokens",
  "output_tokens","total_tokens","priced_requests","estimated_cost_usd",
  "by_model","by_status","recent"},usage
assert usage["requests"]==3 and usage["successes"]==2 and usage["failures"]==1,usage
assert usage["input_tokens"]==1060 and usage["output_tokens"]==545 and usage["total_tokens"]==1605,usage
assert usage["priced_requests"]==2 and abs(usage["estimated_cost_usd"]-0.004007)<1e-9,usage
assert usage["by_model"][0]["provider"]=="openai" and usage["by_model"][0]["model"]=="qwen",usage
assert usage["by_model"][0]["requests"]==2 and usage["by_model"][0]["failures"]==1,usage
assert usage["by_model"][1]["estimated_cost_usd"] is None,usage
assert usage["by_status"]==[{"status_code":429,"count":1}],usage
assert len(usage["recent"])==2 and usage["recent"][0]["created_at"],usage
assert all(r["id"]!="00000000-0000-0000-0000-000000001104" for r in usage["recent"])
assert req("GET","/operations/ai-usage?hours=0")[0]==400
assert req("GET","/operations/ai-usage?recent_limit=201")[0]==400
code,short=req("GET","/operations/ai-usage?hours=1&recent_limit=1")
assert code==200 and short["requests"]==3 and len(short["recent"])==1,(code,short)
# Historical V2-owned published resources only. V1-only objects and unverified
# submissions must remain untouched by this backfill. Never touch prod data.
def published_fixture(suffix,age,unknown=False):
    task=f"00000000-0000-0000-0000-000000002{suffix}"
    workflow=f"00000000-0000-0000-0000-000000003{suffix}"
    key=f"v2/retention-test/{task}/sample.mp4"
    pg(f"""INSERT INTO tasks(id,source_type,source_license,status,created_by,updated_at)
      VALUES ('{task}','local','authorized','published','rust-smoke',
        now()-interval '{age} days');
      INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage)
      VALUES ('{workflow}','{task}',1,'succeeded','cleanup');
      INSERT INTO storage_objects(storage_key,size_bytes,retain_until,state)
      VALUES ('{key}',4,now()+interval '300 days','live');
      INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
      VALUES ('{key}','task','{task}','output');
    """)
    if unknown:
        pg(f"""INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state)
          VALUES('00000000-0000-0000-0000-000000004{suffix}',
          '{workflow}','{task}','publish',1,'retention-unknown-{suffix}','unknown');""")
    return task,workflow,key
old=published_fixture("101",3)
recent=published_fixture("102",0)
uncertain=published_fixture("103",4,True)
sidecar=published_fixture("104",4)
manual_dub=published_fixture("105",4)
pg(f"""INSERT INTO operation_inbox
 (id,operation_key,runtime_engine,task_id,stage,generation,status,
  sau_platform,sau_account_id,sau_video_key,sau_fingerprint,attempt_count,max_attempts)
 VALUES('00000000-0000-0000-0000-000000009104',
        'v2:sau:intent:00000000-0000-0000-0000-000000009104','rust-v2',
        '{sidecar[0]}','sau_publish',0,'unknown','douyin',
        '00000000-0000-0000-0000-000000009105','{sidecar[2]}',
        'test-retention-sidecar-104',1,1);
 INSERT INTO dubbing_jobs(id,task_id,status,progress,request_json)
 VALUES('00000000-0000-0000-0000-000000009106',
        '{manual_dub[0]}','queued',0,'{{"model_path":"legacy:cosyvoice"}}'::jsonb);""")
def count(sql):
    return int(pg(sql,True))
assert req("GET","/settings/storage")[1]=={"asset_ttl_days":0}
subprocess.run(["./target/debug/videoroll-v2","gc","--limit","50"],check=True,stdout=subprocess.DEVNULL)
assert count(f"SELECT count(*) FROM storage_object_refs WHERE storage_key='{old[2]}'")==1
assert count(f"SELECT count(*) FROM workflow_events WHERE workflow_id='{old[1]}' AND kind='storage.cleanup_scheduled'")==0
req("PUT","/settings/storage",{"asset_ttl_days":1})
subprocess.run(["./target/debug/videoroll-v2","gc","--limit","50"],check=True,stdout=subprocess.DEVNULL)
assert count(f"SELECT count(*) FROM storage_object_refs WHERE storage_key='{old[2]}'")==0
assert count(f"SELECT count(*) FROM workflow_events WHERE workflow_id='{old[1]}' AND kind='storage.cleanup_scheduled'")==1
assert count(f"SELECT count(*) FROM storage_object_refs WHERE storage_key='{recent[2]}'")==1
assert count(f"SELECT count(*) FROM storage_object_refs WHERE storage_key='{uncertain[2]}'")==1
assert count(f"SELECT count(*) FROM storage_object_refs WHERE storage_key='{sidecar[2]}'")==1
assert count(f"SELECT count(*) FROM storage_object_refs WHERE storage_key='{manual_dub[2]}'")==1
assert count(f"SELECT count(*) FROM workflow_events WHERE workflow_id='{uncertain[1]}' AND kind='storage.cleanup_scheduled'")==0
# Re-run to verify no duplicate cleanup event and explicit storage lease.
subprocess.run(["./target/debug/videoroll-v2","gc","--limit","50"],check=True,stdout=subprocess.DEVNULL)
assert count(f"SELECT count(*) FROM workflow_events WHERE workflow_id='{old[1]}' AND kind='storage.cleanup_scheduled'")==1
req("PUT","/settings/storage",{"asset_ttl_days":0})
print("PASS: V2 published backfill is TTL-gated and idempotent; recent, unknown job, pending SAU and active dubbing inputs retained")

print("PASS: 5 native V1 operations/storage routes: auth, v1 fallback, v2-only writes, validation, SQL aggregation, micro-USD model pricing and recent windows")
