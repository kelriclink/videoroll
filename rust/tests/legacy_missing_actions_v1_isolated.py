"""Native V1 youtube enqueue and automatic dubbing: real DB jobs, idempotency, no external publish."""
from uuid import uuid4
import os,json
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text

uri=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in uri and uri.endswith('/videoroll_test')
engine=create_engine(uri)
base=os.getenv('VIDEOROLL_TEST_V1_BASE','http://127.0.0.1:18080/api/v1')
assert base.startswith('http://127.0.0.1:') and base.endswith('/api/v1')
hdr={'x-videoroll-v2-internal-token':os.environ['VIDEOROLL_V2_INTERNAL_TOKEN'],'content-type':'application/json'}
def post(path,payload=None):
    data=json.dumps({} if payload is None else payload).encode()
    try:r=urlopen(Request(base+path,data=data,method='POST',headers=hdr),timeout=12)
    except HTTPError as e:r=e
    with r:
        b=r.read()
        try:body=json.loads(b)
        except ValueError:body=b.decode(errors='replace')
        return r.status,body

task=uuid4()
with engine.begin() as db:
    db.execute(text("""INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner,runtime_origin)
       VALUES(CAST(:id AS uuid),'youtube','https://www.youtube.com/watch?v=0123456789A','authorized','created','rust','youtube_manual')"""),{'id':str(task)})
status,body=post(f'/tasks/{task}/actions/youtube_download_enqueue')
assert status==200 and body=={'status':'queued','task_id':str(task)},(status,body)
with engine.connect() as db:
    jobs=db.execute(text("SELECT id,status,workflow_id FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(task)}).all()
assert len(jobs)==1 and jobs[0].status=='pending',jobs
with engine.connect() as db:
    flow=db.execute(text("SELECT configuration FROM vr2_workflows WHERE id=:id"),{'id':jobs[0].workflow_id}).scalar()
    assert flow['manual_download_only'] and flow['manual_download_enqueued']
    assert db.execute(text("SELECT max_attempts FROM vr2_jobs WHERE id=:id"),{'id':jobs[0].id}).scalar()==4
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
      {'config':json.dumps({'auto_publish':False,'auto_dub_p2':True,'auto_publish_platforms':[]})})
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
# With no publication target, the enabled dubbing stage still checks speech.
with engine.begin() as db:
    db.execute(text("UPDATE app_settings SET value_json=value_json||CAST(:config AS jsonb) WHERE key='subtitle.auto_profile'"),{"config":json.dumps({"auto_publish":True,"auto_publish_platforms":["douyin"]})})
status,body=post(f'/tasks/{auto}/actions/auto_dubbing')
assert status==200 and 'no speech' in body['detail'],(status,body)
with engine.begin() as db:
    db.execute(text("UPDATE app_settings SET value_json=value_json||CAST(:config AS jsonb) WHERE key='subtitle.auto_profile'"),{"config":json.dumps({"auto_dub_p2":False})})
status,body=post(f'/tasks/{auto}/actions/auto_dubbing')
assert status==200 and 'disabled' in body['detail'],(status,body)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='dubbing'"),{'id':str(auto)}).scalar()==0

# Direct original SubtitleService job creation must enter the real durable Rust
# subtitle stage rather than leaving an orphan `subtitle_jobs` row for Hatchet.
subtask=uuid4(); raw_key=f'v2/test/raw/{subtask}.mp4'; raw_id=uuid4()
with engine.begin() as db:
    db.execute(text("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,runtime_origin)
        VALUES(CAST(:id AS uuid),'local','own','created','rust','upload_manual')"""),{'id':str(subtask)})
    db.execute(text("""INSERT INTO assets(id,task_id,kind,storage_key)
        VALUES(CAST(:asset AS uuid),CAST(:id AS uuid),'video_raw',:key)"""),
        {'asset':str(raw_id),'id':str(subtask),'key':raw_key})
job_request={'task_id':str(subtask),'input':{'type':'storage','key':raw_key},
    'prefer_youtube_subtitles':False,'youtube_subtitle_mode':'off',
    'asr':{'engine':'auto','language':'en'},
    'translate':{'enabled':False,'provider':'mock','target_lang':'zh'},
    'output':{'formats':['srt'],'render':{'burn_in':False}}}
for invalid in [{**job_request,'input':{'type':'storage','key':'v2/other-task/raw.mp4'}},
                {**job_request,'youtube_subtitle_mode':'invalid'},
                {**job_request,'selective_retranslate':{'indices':[1]}},
                {**job_request,'asr':{'engine':'groq-whisper'}}]:
    status,body=post('/subtitle/jobs',invalid)
    assert status in (400,409),(invalid,status,body)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='subtitle'"),{'id':str(subtask)}).scalar()==0
status,body=post('/subtitle/jobs',job_request)
assert status==200 and body['status']=='queued',(status,body)
subtitle_id=body['job_id']
with engine.connect() as db:
    row=db.execute(text("""SELECT id,status,workflow_id,operation_key FROM operation_inbox
        WHERE task_id=CAST(:id AS uuid) AND stage='subtitle'"""),{'id':str(subtask)}).one()
    assert str(row.id)==subtitle_id and row.status=='pending',row
    assert row.operation_key.startswith('vr2:'),row
    assert db.execute(text("SELECT count(*) FROM subtitle_jobs WHERE task_id=CAST(:id AS uuid)"),{'id':str(subtask)}).scalar()==0
status,second=post('/subtitle/jobs',job_request)
assert status==200 and second==body,(status,second)
# Repeated calls during an active native stage must reuse it even when the
# worker already wrote the SRT but has not yet completed the stage lease.
with engine.begin() as db:
    db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:asset AS uuid),CAST(:id AS uuid),'subtitle_srt',:key)"),
        {'asset':str(uuid4()),'id':str(subtask),'key':f'v2/test/sub/{subtask}.srt'})
status,during_output=post('/subtitle/jobs',job_request)
assert status==200 and during_output==body,(status,during_output)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='subtitle'"),{'id':str(subtask)}).scalar()==1
status,conflict=post('/subtitle/jobs',{**job_request,'translate':{'enabled':True,'provider':'mock'}})
assert status==409,(status,conflict)
req=Request(base+'/subtitle/jobs/'+subtitle_id,headers=hdr)
with urlopen(req,timeout=12) as r:
    detail=json.loads(r.read())
assert detail['job_id']==subtitle_id and detail['task_id']==str(subtask) and detail['status']=='queued',detail
# Preserve original active Python/Hatchet jobs; no duplicate Rust job.
legacy_task=uuid4(); legacy_sub=uuid4()
with engine.begin() as db:
    db.execute(text("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner)
         VALUES(CAST(:task AS uuid),'local','own','created','rust')"""),{'task':str(legacy_task)})
    db.execute(text("""INSERT INTO subtitle_jobs(id,task_id,status,request_json)
         VALUES(CAST(:job AS uuid),CAST(:task AS uuid),'queued','{}'::jsonb)"""),
         {'job':str(legacy_sub),'task':str(legacy_task)})
status,legacy=post('/subtitle/jobs',{**job_request,'task_id':str(legacy_task)})
assert status==200 and legacy=={'job_id':str(legacy_sub),'status':'queued'},(status,legacy)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid)"),{'id':str(legacy_task)}).scalar()==0
print('PASS: V1 /subtitle/jobs schedules real native subtitle operation, preserves original active jobs, idempotency, stage GET and fails closed for unsupported inputs')

print('PASS: original V1 youtube enqueue is durable and idempotent; automatic P2 rejects manual tasks, skips disabled/no-speech without creating jobs')

# A fresh explicit download must not need a second click or a new route.
# Use the isolated yt-dlp fixture, actual FFprobe and real HTTP response.
manual_task=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner,error_code,error_message) VALUES(CAST(:id AS uuid),'youtube','https://youtu.be/0123456789A','authorized','failed','rust','YOUTUBE_DOWNLOAD_FAILED','old error')"),{'id':str(manual_task)})
status,rejected=post(f'/tasks/{manual_task}/actions/youtube_download_enqueue')
assert status==409 and rejected['detail']['code']=='YOUTUBE_DOWNLOAD_ENQUEUE_REJECTED',(status,rejected)
status,downloaded=post(f'/tasks/{manual_task}/actions/youtube_download')
assert status==200 and downloaded['metadata']['title']=='English title of test',(status,downloaded)
from videoroll.apps.orchestrator_api.schemas import YouTubeDownloadActionResponse
YouTubeDownloadActionResponse.model_validate(downloaded)
with engine.connect() as db:
    assert db.execute(text("SELECT status::text,error_code,error_message FROM tasks WHERE id=CAST(:id AS uuid)"),{'id':str(manual_task)}).one()==('downloaded',None,None)
    assert db.execute(text("SELECT stage,status FROM operation_inbox WHERE task_id=CAST(:id AS uuid)"),{'id':str(manual_task)}).all()==[('download','succeeded')]
    assert db.execute(text("SELECT source_id,uploader,duration FROM youtube_video_meta WHERE task_id=CAST(:id AS uuid)"),{'id':str(manual_task)}).one()==('0123456789A','Test Channel',23)
print('PASS: original synchronous manual retry downloads, returns DTO, clears old failure, persists source metadata and does not start subtitle/publish')

# The old synchronous download action returns complete assets ONLY when they
# exist on disk, and it never pretends a queued Rust stage is a finished file.
from pathlib import Path
for _ in range(2):
    status,manual=post(f'/tasks/{task}/actions/youtube_download')
    assert status==409 and manual['detail']['code']=='YOUTUBE_DOWNLOAD_IN_PROGRESS',(status,manual)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(task)}).scalar()==1
# Simulate a finished Rust worker receipt using only disposable test files.
root=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT'])
raw_key=f'v2/raw/{task}/source.mp4'
meta_key=f'v2/youtube-meta/{task}/metadata.json'
raw_file=root/raw_key
meta_file=root/meta_key
raw_file.parent.mkdir(parents=True,exist_ok=True)
meta_file.parent.mkdir(parents=True,exist_ok=True)
raw_file.write_bytes(b'isolated-test-video-file-not-a-real-download')
meta={'id':'0123456789A','title':'VideoRoll mock video','description':'Isolated fixture',
      'webpage_url':'https://www.youtube.com/watch?v=0123456789A','duration':42,'uploader':'fixture channel'}
meta_file.write_text(json.dumps(meta))
raw_id=uuid4()
meta_id=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key,size_bytes) VALUES(CAST(:asset AS uuid),CAST(:id AS uuid),'video_raw',:key,44)"),
        {'asset':str(raw_id),'id':str(task),'key':raw_key})
    db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key,size_bytes) VALUES(CAST(:asset AS uuid),CAST(:id AS uuid),'metadata_json',:key,210)"),
        {'asset':str(meta_id),'id':str(task),'key':meta_key})
status,complete=post(f'/tasks/{task}/actions/youtube_download')
assert status==200 and complete['metadata']['title']=='VideoRoll mock video',(status,complete)
assert complete['video_asset']['id']==str(raw_id) and complete['metadata_asset']['id']==str(meta_id),complete
assert complete['cover_asset'] is None and complete['video_asset']['storage_key']==raw_key,complete
from videoroll.apps.orchestrator_api.schemas import YouTubeDownloadActionResponse
YouTubeDownloadActionResponse.model_validate(complete)
meta_file.write_text('broken JSON')
status,corrupt=post(f'/tasks/{task}/actions/youtube_download')
assert status==400,(status,corrupt)
meta_file.write_text(json.dumps(meta))
# Original Python in-flight operation must block a second Rust dispatch.
python_task=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(CAST(:id AS uuid),'youtube','https://youtu.be/0123456789B','authorized','created','rust')"),{'id':str(python_task)})
    db.execute(text("INSERT INTO operation_inbox(id,operation_key,status) VALUES(CAST(:op AS uuid),:key,'processing')"),
        {'op':str(uuid4()),'key':'youtube-download:'+str(python_task)})
status,conflict=post(f'/tasks/{python_task}/actions/youtube_download')
assert status==409 and conflict['detail']['code']=='YOUTUBE_DOWNLOAD_IN_PROGRESS',(status,conflict)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM vr2_jobs WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(python_task)}).scalar()==0
print('PASS: V1 synchronous YouTube download returns validated original DTO when ready, conflicts only on existing work, protects Python lease and corrupt metadata')

# Original Python writes the fallback download progress in app_settings and
# operation_inbox; after replacement a historical job must not become idle.
from videoroll.apps.orchestrator_api.schemas import YouTubeDownloadProgressRead
with engine.begin() as db:
    db.execute(text("INSERT INTO app_settings(key,value_json) VALUES(:key,CAST(:value AS jsonb))"),
        {'key':'youtube.download_progress.'+str(python_task),'value':json.dumps({'status':'downloading','active':True,'progress':47,'downloaded_bytes':470,'total_bytes':1000,'filename':'fixture.mp4'})})
    db.execute(text("UPDATE operation_inbox SET lease_until=now()-interval '1 minute' WHERE operation_key=:key"),{'key':'youtube-download:'+str(python_task)})
    db.execute(text("UPDATE tasks SET runtime_owner='python' WHERE id=CAST(:id AS uuid)"),{'id':str(python_task)})
def get_progress(t):
    with urlopen(Request(base+f'/tasks/{t}/youtube_download_progress',headers=hdr),timeout=12) as r:
        return YouTubeDownloadProgressRead.model_validate(json.load(r))
progress=get_progress(python_task)
assert progress.status=='queued' and not progress.active and progress.progress==47 and progress.downloaded_bytes==470,progress
with engine.begin() as db:
    db.execute(text("UPDATE operation_inbox SET status='completed' WHERE operation_key=:key"),{'key':'youtube-download:'+str(python_task)})
progress=get_progress(python_task)
assert progress.status=='completed' and not progress.active and progress.progress==100 and progress.error is None,progress
print('PASS: original PostgreSQL historical progress and expired download lease projection')
# A stage whose receipt was persisted but whose media object is missing must
# never be marked completed or replayed automatically: reconcile the storage.
lost_task=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(CAST(:id AS uuid),'youtube','https://youtu.be/0123456789C','authorized','created','rust')"),{'id':str(lost_task)})
status,queued=post(f'/tasks/{lost_task}/actions/youtube_download_enqueue')
assert status==200 and queued['status']=='queued',(status,queued)
with engine.begin() as db:
    db.execute(text("UPDATE operation_inbox SET status='succeeded' WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(lost_task)})
status,recovery=post(f'/tasks/{lost_task}/actions/youtube_download')
assert status==409 and recovery['detail']['code']=='YOUTUBE_DOWNLOAD_RESULT_RECOVERING',(status,recovery)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(lost_task)}).scalar()==1
print('PASS: lost successful download cannot claim completion or dispatch an unsafe retry')

# Original task list/detail and assets must include unadopted historical rows,
# using their original object keys without rewriting the existing files.
historical=uuid4(); old_meta=uuid4(); old_final=uuid4(); old_cover=uuid4()
old_dir=root/'raw'/str(historical)
old_dir.mkdir(parents=True)
(old_dir/'metadata.json').write_text(json.dumps({'fulltitle':'Historical original title'}))
(old_dir/'final.mp4').write_bytes(b'0123456789abcdef')
(old_dir/'cover.jpg').write_bytes(b'fixture-cover')
with engine.begin() as db:
    db.execute(text("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(:id,'youtube','authorized','rendered','python')"),{'id':historical})
    for aid,kind,name in [(old_meta,'metadata_json','metadata.json'),(old_final,'video_final','final.mp4'),(old_cover,'cover_image','cover.jpg')]:
        db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(:aid,:tid,CAST(:kind AS asset_kind),:key)"),{'aid':aid,'tid':historical,'kind':kind,'key':f'raw/{historical}/{name}'})
def get(path,extra=None):
    with urlopen(Request(base+path,headers={**hdr,**(extra or {})}),timeout=12) as r:
        return r.status,r.read()
detail=json.loads(get(f'/tasks/{historical}')[1])
assert detail['display_title']=='Historical original title' and detail['status']=='RENDERED',detail
rows=json.loads(get('/tasks?status=RENDERED&limit=500')[1])
assert any(x['id']==str(historical) and x['display_title']=='Historical original title' for x in rows),rows
assets=json.loads(get(f'/tasks/{historical}/assets')[1])
assert {a['id'] for a in assets}=={str(old_meta),str(old_final),str(old_cover)},assets
converted=json.loads(get('/videos/converted?limit=200')[1])
assert any(x['task']['id']==str(historical) and x['final_asset']['id']==str(old_final) and x['cover_asset']['id']==str(old_cover) for x in converted)
assert get(f'/tasks/{historical}/assets/{old_final}/stream',{'Range':'bytes=2-5'})==(206,b'2345')
try:
    get(f'/tasks/{task}/assets/{old_final}/stream')
    raise AssertionError('cross-task media access must not succeed')
except HTTPError as e:
    assert e.code==404
with engine.connect() as db:
    assert db.execute(text("SELECT runtime_owner FROM tasks WHERE id=:id"),{'id':historical}).scalar()=='python'
    assert db.execute(text("SELECT count(*) FROM vr2_jobs WHERE task_id=:id"),{'id':historical}).scalar()==0
print('PASS: original historical task, title, assets, converted video and Range stream, without adoption or new jobs')
