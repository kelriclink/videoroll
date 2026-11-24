"""Disposable DB/HTTP/UPOS Bilibili direct posting regression."""
import json,os,subprocess,time
from pathlib import Path
from uuid import uuid4
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text

uri=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in uri and uri.endswith('/videoroll_test')
engine=create_engine(uri)
root=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT'])
headers={'x-videoroll-v2-internal-token':os.environ['VIDEOROLL_V2_INTERNAL_TOKEN'],'content-type':'application/json'}
def api(path,data=None):
    req=Request('http://127.0.0.1:18080/api/v1'+path,headers=headers,
       data=json.dumps(data).encode() if data is not None else None,
       method='POST' if data is not None else 'GET')
    try:r=urlopen(req,timeout=15)
    except HTTPError as e:r=e
    with r:return r.status,json.loads(r.read())
with engine.begin() as db:
    config={'cookie_enc':'dbplain:v1:SESSDATA=video-roll-test; bili_jct=1234567890123456'}
    for key,body in [('bilibili.auth',config),
                     ('publish.review.settings',{'enabled':False,'blocked_words':[],'ai_rules':''}),
                     ('publish.platforms',{'bilibili':True})]:
        db.execute(text('INSERT INTO app_settings(key,value_json,version) VALUES(:key,CAST(:value AS jsonb),1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json'),
            {'key':key,'value':json.dumps(body)})
def fixture(title):
    task=uuid4()
    key=f'v2/raw/{task}/source.mp4'
    file=root/key;file.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-f','lavfi','-i','color=c=blue:s=320x180:r=25','-t','1.1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(file)],check=True,timeout=30)
    with engine.begin() as db:
        db.execute(text("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(CAST(:id AS uuid),'youtube','https://youtu.be/0123456789A','authorized','rendered','rust')"),{'id':str(task)})
        db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:asset AS uuid),CAST(:id AS uuid),'video_final',:key)"),{'asset':str(uuid4()),'id':str(task),'key':key})
    payload={'task_id':str(task),'video':{'type':'storage','key':key},
      'typeid_mode':'bilibili_predict' if title=='isolated Rust P1' else 'meta',
      'meta':{'title':title,'desc':'licensed source','typeid':171,'tags':['science'],
              'copyright':2,'source':'https://youtu.be/0123456789A'}}
    return task,payload

def check(task,status):
    end=time.monotonic()+75
    while time.monotonic()<end:
        with engine.connect() as db:
            value=db.execute(text('SELECT state::text FROM publish_jobs WHERE task_id=CAST(:id AS uuid)'),{'id':str(task)}).scalar_one()
        if value==status:return
        if value not in ('submitting',):raise AssertionError((value,status))
        time.sleep(.6)
    raise AssertionError(('publisher did not reach',status))

task,payload=fixture('isolated Rust P1')
# Original Python cover InputRef must become a real Bilibili CDN URL.
import base64
cover_key=f'v2/cover/{task}/cover.png'
cover_file=root/cover_key
cover_file.parent.mkdir(parents=True,exist_ok=True)
cover_file.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL2XQAAAABJRU5ErkJggg=='))
with engine.begin() as db:
    db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:asset AS uuid),CAST(:task AS uuid),'cover_image',:key)"),
        {'asset':str(uuid4()),'task':str(task),'key':cover_key})
payload['cover']={'type':'storage','key':cover_key}
for alt in [dict(payload,force_retry=True),dict(payload,account_id=str(uuid4())),
            dict(payload,batch_id=str(uuid4())),dict(payload,typeid_mode='ai_summary')]:
    code,_=api('/bilibili/publish',alt)
    assert code==409,code
code,receipt=api('/bilibili/publish',payload)
assert code==200 and receipt['state']=='submitting',(code,receipt)
job=receipt['job_id']
assert api('/bilibili/publish',payload)==(code,receipt)
with engine.connect() as db:
    row=db.execute(text('SELECT operation_key FROM publish_jobs WHERE id=CAST(:id AS uuid)'),{'id':job}).scalar_one()
    assert row=='publish-job:'+job,row
    assert db.execute(text('SELECT status FROM operation_inbox WHERE operation_key=:key'),{'key':row}).scalar_one()=='processing'
    assert db.execute(text('SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:task AS uuid) AND stage=\'publish\''),{'task':str(task)}).scalar_one()==1
print('PASS: original publish_jobs and Python recovery fence persisted atomically with native publish stage')

worker=Path(__file__).resolve().parents[1]/'target/debug/videoroll-v2'
log=Path(__file__).resolve().parents[1]/'target/bili-direct-worker.log'
def process(task,final):
    with log.open('w') as output:
        proc=subprocess.Popen([str(worker),'worker'],env={**os.environ,'VIDEOROLL_V2_WORKERS':'1','RUST_LOG':'warn'},stdout=output,stderr=subprocess.STDOUT)
        try:
            check(task,final)
        finally:
            proc.terminate()
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
process(task,'published')
code,final=api('/bilibili/publish',payload)
assert code==200 and final['job_id']==job and final['state']=='published',(code,final)
assert final['bvid']=='BV1RUSTMOCK1' and final['aid']=='56321',final
assert api('/bilibili/publish/jobs/'+job)[1]['state']=='published'
with engine.connect() as db:
    assert db.execute(text('SELECT status FROM operation_inbox WHERE operation_key=:key'),{'key':'publish-job:'+job}).scalar_one()=='completed'
    assert db.execute(text("SELECT status FROM operation_inbox WHERE task_id=CAST(:task AS uuid) AND stage='publish' AND runtime_engine='rust-v2'"),{'task':str(task)}).scalar_one()=='succeeded'
def counters():return json.load(urlopen('http://127.0.0.1:18199/__state'))
counts=counters()
assert counts['submit']==1 and counts['predict']==1 and counts['complete']==1 and counts['chunks']>=1 and counts['cover']==1,counts
print('PASS: native UPOS upload, Bilibili partition prediction, single submit, original published receipt and idempotent HTTP replay')

ambiguous,other=fixture('ambiguous-publish')
code,pending=api('/bilibili/publish',other)
assert code==200 and pending['state']=='submitting',(code,pending)
process(ambiguous,'unknown')
code,uncertain=api('/bilibili/publish',other)
assert code==200 and uncertain['state']=='unknown' and uncertain['job_id']==pending['job_id'],(code,uncertain)
with engine.connect() as db:
    assert db.execute(text('SELECT status FROM operation_inbox WHERE operation_key=:key'),{'key':'publish-job:'+pending['job_id']}).scalar_one()=='completed'
    assert db.execute(text("SELECT status FROM operation_inbox WHERE task_id=CAST(:task AS uuid) AND runtime_engine='rust-v2' AND stage='publish'"),{'task':str(ambiguous)}).scalar_one()=='unknown'
    assert db.execute(text('SELECT count(*) FROM publish_jobs WHERE task_id=CAST(:task AS uuid)'),{'task':str(ambiguous)}).scalar_one()==1
    task_state=db.execute(text('SELECT status::text,error_code FROM tasks WHERE id=CAST(:task AS uuid)'),{'task':str(ambiguous)}).one()
    assert task_state.status=='failed' and task_state.error_code=='PUBLISH_OUTCOME_UNKNOWN',task_state
counts=counters()
assert counts['submit']==2 and counts['ambiguous_submit']==1,counts
print('PASS: ambiguous archive POST quarantined as unknown in original publish_jobs and operation_inbox without duplicate archive')

# Python's `video_parts` requires exactly two task-owned media objects, with
# a confirmed YouDub receipt matching the original source plus current SRT.
two_task,two=fixture('two-part')
video_key=two['video']['key']
dub_key=f'v2/dub/{two_task}/dub.mp4'
srt_key=f'v2/sub/{two_task}/subtitle.srt'
(root/dub_key).parent.mkdir(parents=True,exist_ok=True)
(root/srt_key).parent.mkdir(parents=True,exist_ok=True)
import shutil
shutil.copyfile(root/video_key,root/dub_key)
(root/srt_key).write_text('1\n00:00:00,000 --> 00:00:01,000\n中文翻译\n\n')
two['video_parts']=[{'video':two['video'],'title':'P1中文字幕版'},
    {'video':{'type':'storage','key':dub_key},'title':'P2中文配音版'}]
with engine.begin() as db:
    for kind,key in [('video_raw',video_key),('video_dubbed',dub_key),('subtitle_srt',srt_key)]:
        db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:asset AS uuid),CAST(:task AS uuid),CAST(:kind AS asset_kind),:key)"),
            {'asset':str(uuid4()),'task':str(two_task),'kind':kind,'key':key})
code,unverified=api('/bilibili/publish',two)
assert code==409,(code,unverified)
with engine.connect() as db:
    assert db.execute(text('SELECT count(*) FROM publish_jobs WHERE task_id=CAST(:task AS uuid)'),{'task':str(two_task)}).scalar_one()==0
# A finished Rust dubbing stage is projected from ORIGINAL operation_inbox.
dub_flow=uuid4()
dub_job=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,request_json) VALUES(CAST(:flow AS uuid),CAST(:task AS uuid),1,'rust-v2','videoroll.rust.pipeline','succeeded','{}'::jsonb)"),
        {'flow':str(dub_flow),'task':str(two_task)})
    db.execute(text("""INSERT INTO operation_inbox(
      id,operation_key,status,runtime_engine,workflow_id,task_id,stage,generation,
      dubbing_source_video_key,dubbing_source_srt_key,dubbing_output_video_key,
      dubbing_model_status,dubbing_model_job_id,dubbing_run_created_at)
      VALUES(CAST(:id AS uuid),:operation,'succeeded','rust-v2',CAST(:flow AS uuid),CAST(:task AS uuid),'dubbing',1,
      :raw,:srt,:dub,'succeeded',CAST(:model AS uuid),now())"""),{
        'id':str(dub_job),'operation':'test:dubbing:'+str(dub_job),'flow':str(dub_flow),
        'task':str(two_task),'raw':video_key,'srt':srt_key,'dub':dub_key,'model':str(uuid4())})
code,two_queued=api('/bilibili/publish',two)
assert code==200 and two_queued['state']=='submitting',(code,two_queued)
process(two_task,'published')
code,two_result=api('/bilibili/publish',two)
assert code==200 and two_result['state']=='published' and two_result['job_id']==two_queued['job_id'],(code,two_result)
counts=counters()
assert counts['submit']==3 and counts['complete']==4 and counts['predict']==1,counts
print('PASS: atomic P1/P2 original request requires matching succeeded YouDub receipt, verifies original asset ownership, and submits exactly two ordered Bilibili parts once')

# Restarting a disconnected Rust process must not replay a Bilibili archive
# whose external side effect could already have succeeded.
with log.open('a') as output:
    resumed=subprocess.Popen([str(worker),'worker'],env={**os.environ,'VIDEOROLL_V2_WORKERS':'1','RUST_LOG':'warn'},stdout=output,stderr=subprocess.STDOUT)
    try:time.sleep(4)
    finally:
        resumed.terminate()
        try:resumed.wait(timeout=8)
        except subprocess.TimeoutExpired:resumed.kill();resumed.wait(timeout=5)
after_restart=counters()
assert after_restart['submit']==counts['submit'],after_restart
print('PASS: restarting the Rust worker cannot resubmit an unknown Bilibili archive')
