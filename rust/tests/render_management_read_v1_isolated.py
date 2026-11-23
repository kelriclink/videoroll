#!/usr/bin/env python3
"""Original Python RenderWorker admin read-model on a disposable PostgreSQL."""
import json,os,subprocess
from urllib.request import Request,urlopen
from urllib.error import HTTPError

CONTAINER=os.environ["container_name"]
BASE="http://127.0.0.1:18080/api/v1"
TOKEN=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
def request(path,auth=True):
    headers={"x-videoroll-v2-internal-token":TOKEN} if auth else {}
    req=Request(BASE+path,headers=headers)
    try:
        with urlopen(req,timeout=8) as r: return r.status,json.loads(r.read())
    except HTTPError as e: return e.code,json.loads(e.read())
def pg(sql):
    subprocess.run(["docker","exec","-i",CONTAINER,"psql","-v","ON_ERROR_STOP=1",
      "-U","postgres","-d","videoroll_test"],input=sql,text=True,
      stdout=subprocess.DEVNULL,check=True)
task_id="00000000-0000-0000-0000-000000006101"
worker_id="00000000-0000-0000-0000-000000006102"
worker2_id="00000000-0000-0000-0000-000000006103"
enroll_id="00000000-0000-0000-0000-000000006104"
job_id="00000000-0000-0000-0000-000000006105"
exec_id="00000000-0000-0000-0000-000000006106"
pg("""INSERT INTO tasks(id,source_type,source_license,status)
VALUES ('00000000-0000-0000-0000-000000006101','local','own','created');
INSERT INTO render_workers(
 id,worker_key,name,platform,architecture,version,protocol_version,
 render_spec_versions,capabilities,resources,labels,status,enabled,
 draining,max_concurrency,active_jobs,credential_hash,last_seen_at)
VALUES (
 '00000000-0000-0000-0000-000000006102','render-v1-test','Render Test','windows',
 'x86_64','1.2',1,'[1]'::jsonb,'{"encoders":["av1_qsv"]}'::jsonb,
 '{"devices":[{"id":"GPU0","status":"busy"},{"id":"GPU1","status":"idle"}]}'::jsonb,
 '{"disabled_render_device_ids":["GPU1"]}'::jsonb,
 'busy',TRUE,FALSE,3,1,repeat('f',64),now()-interval '15 seconds'),
 (
 '00000000-0000-0000-0000-000000006103','render-v1-offline','Offline','linux',
 'x86_64','1.1',1,'[1]'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
 'online',TRUE,FALSE,2,0,NULL,now()-interval '10 minutes');
INSERT INTO render_worker_enrollments(id,label,status,expires_at,worker_id)
VALUES ('00000000-0000-0000-0000-000000006104','Windows render invite','consumed',
 now()+interval '1 day','00000000-0000-0000-0000-000000006102');
INSERT INTO render_jobs(id,task_id,status)
VALUES('00000000-0000-0000-0000-000000006105','00000000-0000-0000-0000-000000006101','running');
INSERT INTO render_executions(
 id,render_job_id,worker_id,attempt,state,transfer_mode,progress,
 render_spec,metrics,log_tail,started_at,heartbeat_at)
VALUES (
 '00000000-0000-0000-0000-000000006106',
 '00000000-0000-0000-0000-000000006105',
 '00000000-0000-0000-0000-000000006102',1,'running','http',47,
 '{"schema_version":1,"mode":"burn_in"}'::jsonb,
 '{"fps":18.5}'::jsonb,'frame 47',now()-interval '30 seconds',now()
);
""")
for path in ["/render-management/workers","/render-management/enrollments",
 "/render-management/executions",f"/render-management/executions/{exec_id}"]:
    assert request(path,False)[0]==401,path
code,workers=request("/render-management/workers")
assert code==200 and len(workers)==2,(code,workers)
online=next(w for w in workers if w["id"]==worker_id)
offline=next(w for w in workers if w["id"]==worker2_id)
assert (online["available_slots"],online["device_count"],online["detected_capacity"],online["effective_capacity"])==(2,2,2,3),online
assert online["credential_active"] and not online["stale"],online
assert online["resources"]["devices"][0]["enabled"] is True
assert online["resources"]["devices"][0]["status"]=="busy"
assert online["resources"]["devices"][1]["enabled"] is False
assert online["resources"]["devices"][1]["status"]=="disabled"
assert offline["stale"] and not offline["credential_active"] and offline["device_count"]==0,offline
code,enrollments=request("/render-management/enrollments")
assert code==200 and any(e["id"]==enroll_id and e["worker_id"]==worker_id and e["status"]=="consumed" for e in enrollments),(code,enrollments)
code,executions=request("/render-management/executions")
assert code==200 and len(executions)==1,(code,executions)
row=executions[0]
assert row["id"]==exec_id and row["worker_name"]=="Render Test" and row["task_id"]==task_id,row
assert row["job_status"]=="running" and row["metrics"]=={"fps":18.5}
assert row["log_tail"]=="frame 47" and row["progress"]==47,row
assert request(f"/render-management/executions/{exec_id}")[1]==row
assert request("/render-management/executions?state=succeeded")[1]==[]
assert len(request(f"/render-management/executions?worker_id={worker_id}&task_id={task_id}&limit=1")[1])==1
assert request(f"/render-management/executions?worker_id={worker2_id}")[1]==[]
assert request("/render-management/executions/00000000-0000-0000-0000-000000006199")[0]==404
print("PASS: original RenderWorker admin workers, enrollments, executions and execution detail read model; real stale, devices, worker and task join; full auth")
