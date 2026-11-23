"""Native V1 youtube enqueue and automatic dubbing: real DB jobs, idempotency, no external publish."""
from uuid import uuid4
import os,json
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text

uri=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in uri and uri.endswith('/videoroll_test')
engine=create_engine(uri)
base='http://127.0.0.1:18080/api/v1'
hdr={'x-videoroll-v2-internal-token':os.environ['VIDEOROLL_V2_INTERNAL_TOKEN'],'content-type':'application/json'}
def post(path):
    try:r=urlopen(Request(base+path,data=b'{}',method='POST',headers=hdr),timeout=12)
    except HTTPError as e:r=e
    with r:
        b=r.read()
        try:body=json.loads(b)
        except ValueError:body=b.decode(errors='replace')
        return r.status,body

task=uuid4()
with engine.begin() as db:
    db.execute(text("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,runtime_origin)
       VALUES(CAST(:id AS uuid),'youtube','authorized','created','rust','youtube_manual')"""),{'id':str(task)})
status,body=post(f'/tasks/{task}/actions/youtube_download_enqueue')
assert status==200 and body=={'status':'queued','task_id':str(task)},(status,body)
with engine.connect() as db:
    jobs=db.execute(text("SELECT id,status,workflow_id FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(task)}).all()
assert len(jobs)==1 and jobs[0].status=='pending',jobs
status,body=post(f'/tasks/{task}/actions/youtube_download_enqueue')
assert status==200 and body['status']=='queued',(status,body)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(task)}).scalar()==1
status,body=post(f'/tasks/{task}/actions/auto_dubbing')
assert status==200 and body['status']=='skipped' and body['dubbing_job_id'] is None,(status,body)
with engine.begin() as db:
    db.execute(text("""INSERT INTO app_settings(key,value_json,version)
      VALUES('subtitle.auto_profile',CAST(:config AS jsonb),1)
      ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,version=app_settings.version+1"""),
      {'config':json.dumps({'auto_publish':True,'auto_dub_p2':True,'auto_publish_platforms':['bilibili']})})
status,body=post(f'/tasks/{task}/actions/auto_dubbing')
assert status==409,(status,body)
auto=uuid4(); flow=uuid4(); sub=uuid4()
with engine.begin() as db:
    db.execute(text("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,runtime_origin)
      VALUES(CAST(:id AS uuid),'youtube','authorized','created','rust','youtube_auto')"""),{'id':str(auto)})
    db.execute(text("""INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state,generation,request_json)
      VALUES(CAST(:flow AS uuid),CAST(:id AS uuid),'rust-v2','videoroll.rust.pipeline','succeeded',1,'{}'::jsonb)"""),{'flow':str(flow),'id':str(auto)})
    db.execute(text("""INSERT INTO operation_inbox(id,operation_key,status,runtime_engine,workflow_id,task_id,stage,generation,result_json)
      VALUES(CAST(:sub AS uuid),:key,'succeeded','rust-v2',CAST(:flow AS uuid),CAST(:id AS uuid),'subtitle',1,
      CAST(:result AS jsonb))"""),{'sub':str(sub),'key':'mock:subtitle:'+str(sub),'flow':str(flow),'id':str(auto),'result':json.dumps({'no_speech':True})})
status,body=post(f'/tasks/{auto}/actions/auto_dubbing')
assert status==200 and body['status']=='skipped' and 'no speech' in body['detail'],(status,body)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='dubbing'"),{'id':str(auto)}).scalar()==0
print('PASS: original V1 youtube enqueue is durable and idempotent; automatic P2 rejects manual tasks, skips disabled/no-speech without creating jobs')
