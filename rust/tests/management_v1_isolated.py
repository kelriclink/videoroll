#!/usr/bin/env python3
"""Test V1 YouDub summaries and render connection using disposable fixtures."""
import json,os,subprocess
from urllib.request import Request,urlopen
from urllib.error import HTTPError

BASE="http://127.0.0.1:18080/api/v1"
TOKEN=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
CONTAINER=os.environ["container_name"]
def request(method,path,body=None,auth=True):
    headers={"content-type":"application/json"}
    if auth: headers["x-videoroll-v2-internal-token"]=TOKEN
    q=Request(BASE+path,method=method,headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(q,timeout=8) as res:return res.status,json.loads(res.read())
    except HTTPError as error:return error.code,json.loads(error.read())
def pg(sql,query=False):
    cmd=["docker","exec","-i",CONTAINER,"psql","-At","-v","ON_ERROR_STOP=1",
         "-U","postgres","-d","videoroll_test"]
    if query:
        return subprocess.check_output(cmd+["-c",sql],text=True).strip()
    subprocess.run(cmd,input=sql,text=True,check=True,stdout=subprocess.DEVNULL)

pg("""INSERT INTO tasks(id,source_type,source_license,status) VALUES
 ('00000000-0000-0000-0000-000000007101','local','own','created'),
 ('00000000-0000-0000-0000-000000007102','local','own','created');
 INSERT INTO dubbing_jobs(id,task_id,status,progress,request_json,output_json,error_message,created_at) VALUES
 ('00000000-0000-0000-0000-000000008101','00000000-0000-0000-0000-000000007101',
  'queued',0,'{"model_path":"youdub:voxcpm2"}'::jsonb,'{}'::jsonb,NULL,now()-interval '3 minutes'),
 ('00000000-0000-0000-0000-000000008102','00000000-0000-0000-0000-000000007101',
  'synthesizing',37,'{"model_path":"youdub:voxcpm2"}'::jsonb,'{"youdub":{"stage":"synthesizing"}}'::jsonb,NULL,now()-interval '2 minutes'),
 ('00000000-0000-0000-0000-000000008103','00000000-0000-0000-0000-000000007101',
  'failed',40,'{"model_path":"youdub:voxcpm2"}'::jsonb,'{}'::jsonb,'mock decoder failure',now()-interval '1 minute'),
 ('00000000-0000-0000-0000-000000008104','00000000-0000-0000-0000-000000007102',
  'queued',0,'{"model_path":"legacy:cosyvoice"}'::jsonb,'{}'::jsonb,NULL,now());
 INSERT INTO app_settings(key,value_json) VALUES(
 'render_worker_connection','{"server_url":"https://legacy-render.example.test"}'::jsonb)
 ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json;
""")
assert request("GET","/settings/dubbing/jobs/summary",auth=False)[0]==401
status,dubbing=request("GET","/settings/dubbing/jobs/summary")
assert status==200,(status,dubbing)
assert set(dubbing)=={"queued","running","failed","recent"}
assert (dubbing["queued"],dubbing["running"],dubbing["failed"])==(1,1,1),dubbing
assert len(dubbing["recent"])==3,dubbing
assert dubbing["recent"][0]["error"]=="mock decoder failure",dubbing
assert dubbing["recent"][1]["stage"]=="synthesizing",dubbing
assert all(r["task_id"]=="00000000-0000-0000-0000-000000007101" for r in dubbing["recent"])
assert request("GET","/render-management/connection",auth=False)[0]==401
status,connection=request("GET","/render-management/connection")
assert status==200 and connection=={
 "server_url":"https://legacy-render.example.test","worker_api_path":"/api/render-workers/v1",
 "protocol_version":1},(status,connection)
status,updated=request("PUT","/render-management/connection",
 {"server_url":"https://new-render.example.test/"})
assert status==200 and updated["server_url"]=="https://new-render.example.test",(status,updated)
assert request("GET","/render-management/connection")[1]==updated
assert request("PUT","/render-management/connection",{"server_url":"not-a-url"})[0]==400
assert pg("SELECT value_json->>'server_url' FROM app_settings WHERE key='render_worker_connection'",True)=="https://new-render.example.test"
print("PASS: native YouDub job summary and render worker connection settings, with legacy records preserved")
