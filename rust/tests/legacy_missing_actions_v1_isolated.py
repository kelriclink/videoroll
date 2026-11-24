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
                {**job_request,'youtube_subtitle_mode':'target'},
                {**job_request,'selective_retranslate':{'indices':[1]}},
                {**job_request,'asr':{'engine':'groq-whisper'}}]:
    status,body=post('/subtitle/jobs',invalid)
    assert status in (400,409),(status,body)
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
print('PASS: V1 synchronous YouTube download returns validated original DTO when ready, queues without false success and protects Python lease and corrupt metadata')
# A stage whose receipt was persisted but whose media object is missing must
# never be marked completed or replayed automatically: reconcile the storage.
lost_task=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(CAST(:id AS uuid),'youtube','https://youtu.be/0123456789C','authorized','created','rust')"),{'id':str(lost_task)})
status,queued=post(f'/tasks/{lost_task}/actions/youtube_download')
assert status==409 and queued['detail']['code']=='YOUTUBE_DOWNLOAD_IN_PROGRESS',(status,queued)
with engine.begin() as db:
    db.execute(text("UPDATE operation_inbox SET status='succeeded' WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(lost_task)})
status,recovery=post(f'/tasks/{lost_task}/actions/youtube_download')
assert status==409 and recovery['detail']['code']=='YOUTUBE_DOWNLOAD_RESULT_RECOVERING',(status,recovery)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='download'"),{'id':str(lost_task)}).scalar()==1
print('PASS: lost successful download cannot claim completion or dispatch an unsafe retry')
