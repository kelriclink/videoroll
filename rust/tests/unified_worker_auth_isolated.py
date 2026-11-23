#!/usr/bin/env python3
"""Real HTTP auth/heartbeat/enrollment on the unified original worker tables."""
import hashlib,json,os,uuid
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text

uri=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert uri.endswith('/videoroll_test')
engine=create_engine(uri)
base='http://127.0.0.1:18080'
internal=os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']
def hash_of(s):return hashlib.sha256(s.encode()).hexdigest()
def send(method,path,body=None,bearer=None,admin=False):
    headers={}
    if bearer:headers['Authorization']='Bearer '+bearer
    if admin:headers['x-videoroll-v2-internal-token']=internal
    data=None
    if body is not None:
        headers['Content-Type']='application/json'
        data=json.dumps(body).encode()
    try:response=urlopen(Request(base+path,data=data,method=method,headers=headers),timeout=12)
    except HTTPError as e:response=e
    with response:
        raw=response.read()
        try:body=json.loads(raw)
        except (ValueError,TypeError):body=raw.decode(errors='replace')
        return response.status,body
def select(sql,**kwargs):
    with engine.connect() as db:return db.execute(text(sql),kwargs).scalar()
for name in ('render_workers','render_worker_enrollments','render_executions'):
    assert select("SELECT relkind FROM pg_class WHERE oid=CAST(:name AS regclass)",name='public.'+name)=='r',name
for name in ('vr2_render_workers','vr2_render_enrollments','vr2_render_executions'):
    assert select("SELECT relkind FROM pg_class WHERE oid=CAST(:name AS regclass)",name='public.'+name)=='v',name

old_id=str(uuid.uuid4())
legacy_secret='vrw_'+uuid.uuid4().hex+'-worker-auth'
with engine.begin() as db:
    db.execute(text("""INSERT INTO render_workers
      (id,worker_key,name,platform,status,enabled,credential_hash,credential_issued_at)
      VALUES(CAST(:id AS uuid),:key,'existing Python worker','linux','online',
             true,:hash,now())"""),
      {'id':old_id,'key':'python-worker-'+uuid.uuid4().hex,'hash':hash_of(legacy_secret)})
status,info=send('POST',f'/api/render-workers/v1/{old_id}/heartbeat',
  {'status':'online','resources':{'legacy_ping':True},
   'capabilities':{},'active_execution_ids':[]},bearer=legacy_secret)
assert status==200 and info['id']==old_id and info['resources']['legacy_ping'],(status,info)
assert select("SELECT runtime_engine FROM render_workers WHERE id=CAST(:id AS uuid)",id=old_id)=='python'
assert select("SELECT credential_hash FROM render_workers WHERE id=CAST(:id AS uuid)",id=old_id)==hash_of(legacy_secret)
assert send('POST',f'/api/render-workers/v1/{old_id}/heartbeat',
  {'status':'online','resources':{}},bearer='vrw_'+uuid.uuid4().hex)[0]==401
print('PASS: preexisting vrw_ Python credential authenticates unchanged; no re-enrollment')

status,invite=send('POST','/api/v2/render/enrollments',
  {'label':'native credential test','ttl_minutes':20},admin=True)
assert status==201 and len(invite['token'])==64,(status,invite)
status,enrolled=send('POST','/api/render-workers/v1/enroll',{
    'enrollment_token':invite['token'],
    'worker_key':'native-worker-'+uuid.uuid4().hex,
    'name':'Rust V2 Worker','platform':'linux','version':'native.test',
    'protocol_version':1,'render_spec_versions':[1],
    'max_concurrency':2,'capabilities':{'encoders':['h264']},
    'resources':{},'labels':{}
})
assert status==200 and len(enrolled['credential'])==64,(status,enrolled)
native_id=enrolled['worker']['id'];native_secret=enrolled['credential']
assert select("SELECT runtime_engine FROM render_workers WHERE id=CAST(:id AS uuid)",id=native_id)=='rust-v2'
assert select("SELECT credential_hash FROM render_workers WHERE id=CAST(:id AS uuid)",id=native_id)==hash_of(native_secret)
assert select("SELECT status FROM render_worker_enrollments WHERE id=CAST(:id AS uuid)",id=invite['id'])=='consumed'
assert send('POST','/api/render-workers/v1/enroll',{
    'enrollment_token':invite['token'],
    'worker_key':'other-'+uuid.uuid4().hex,
    'name':'replay','platform':'linux','protocol_version':1,
    'render_spec_versions':[1],'max_concurrency':1,
    'capabilities':{},'resources':{},'labels':{}
})[0]==401
print('PASS: native 64-hex invite creates original-table worker, rotates credential and consumes invitation exactly once')

status,beat=send('POST',f'/api/render-workers/v1/{native_id}/heartbeat',
  {'status':'busy','resources':{'device':'A380'},
   'capabilities':{},'active_execution_ids':[]},bearer=native_secret)
assert status==200 and beat['id']==native_id,(status,beat)
assert select("SELECT status FROM render_workers WHERE id=CAST(:id AS uuid)",id=native_id)=='busy'
assert select("SELECT resources->>'device' FROM render_workers WHERE id=CAST(:id AS uuid)",id=native_id)=='A380'
assert send('POST',f'/api/render-workers/v1/{old_id}/heartbeat',
  {'status':'online','resources':{},'active_execution_ids':[]},bearer=native_secret)[0]==401
assert send('POST',f'/api/render-workers/v1/{native_id}/heartbeat',
  {'status':'online','resources':{},'active_execution_ids':[]},bearer=legacy_secret)[0]==401
assert select("SELECT runtime_engine FROM render_workers WHERE id=CAST(:id AS uuid)",id=native_id)=='rust-v2'
assert select("SELECT runtime_engine FROM render_workers WHERE id=CAST(:id AS uuid)",id=old_id)=='python'
assert select("SELECT count(*) FROM vr2_render_workers WHERE id=CAST(:id AS uuid)",id=old_id)==0
assert select("SELECT count(*) FROM vr2_render_workers WHERE id=CAST(:id AS uuid)",id=native_id)==1
print('PASS: native and original worker heartbeats share one physical table but cannot impersonate each other')

# Legacy vre_ invitations still enroll via the original authentication
# protocol, without removing existing native credentials.
old_invite='vre_'+uuid.uuid4().hex
with engine.begin() as db:
    db.execute(text("""INSERT INTO render_worker_enrollments
      (id,token_hash,label,status,expires_at)
      VALUES(CAST(:id AS uuid),:hash,'original invitation','active',now()+interval '1 hour')"""),
      {'id':str(uuid.uuid4()),'hash':hash_of(old_invite)})
status,legacy_enroll=send('POST','/api/render-workers/v1/enroll',{
   'enrollment_token':old_invite,'worker_key':'old-wire-'+uuid.uuid4().hex,
   'name':'Original protocol new worker','platform':'windows',
   'protocol_version':1,'render_spec_versions':[1],'max_concurrency':1,
   'capabilities':{},'resources':{},'labels':{}
})
assert status==200,(status,legacy_enroll)
assert legacy_enroll['credential'].startswith('vrw_'),legacy_enroll
second_id=legacy_enroll['worker']['id']
assert select("SELECT runtime_engine FROM render_workers WHERE id=CAST(:id AS uuid)",id=second_id)=='python'
assert select("SELECT credential_hash FROM render_workers WHERE id=CAST(:id AS uuid)",id=second_id)==hash_of(legacy_enroll['credential'])
assert send('POST','/api/render-workers/v1/enroll',{
   'enrollment_token':old_invite,'worker_key':'replayed-'+uuid.uuid4().hex,
   'name':'replayed','platform':'windows','protocol_version':1,
   'render_spec_versions':[1],'max_concurrency':1,
   'capabilities':{},'resources':{},'labels':{}
})[0]==401
print('PASS: original vre_ invitation remains accepted, returns vrw_ credential and is single use')
