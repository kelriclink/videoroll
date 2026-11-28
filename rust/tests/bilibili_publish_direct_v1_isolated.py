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
                     ('publish.platforms',{'bilibili':True}),
                     ('subtitle.auto_profile',{'translate_provider':'openai'}),
                     ('subtitle.translate',{'default_provider':'openai','openai':{
                         'model':'mock-bilibili-ai-summary','base_url':'http://127.0.0.1:18199/v1',
                         'timeout_seconds':15}})]:
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

def check(task,status,account_id=None):
    end=time.monotonic()+75
    while time.monotonic()<end:
        with engine.connect() as db:
            value=db.execute(text("SELECT state::text FROM publish_jobs WHERE task_id=CAST(:id AS uuid) AND platform='bilibili' AND account_id IS NOT DISTINCT FROM CAST(:account AS uuid)"),{'id':str(task),'account':account_id}).scalar_one()
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
            dict(payload,batch_id=str(uuid4())),dict(payload,typeid_mode='invalid_mode')]:
    code,_=api('/bilibili/publish',alt)
    assert code in (400,409),code
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
def process(task,final,account_id=None):
    with log.open('w') as output:
        proc=subprocess.Popen([str(worker),'worker'],env={**os.environ,'VIDEOROLL_V2_WORKERS':'1','RUST_LOG':'warn'},stdout=output,stderr=subprocess.STDOUT)
        try:
            check(task,final,account_id)
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

# Use the independently implemented original Python `/archive/type/recommend`
# logic from inside the direct Rust Publisher. The task summary must beat
# caller title/description; a hallucinated type ID must never reach archive.
ai_task,ai_payload=fixture('ai-summary')
ai_payload['typeid_mode']='ai_summary'
with engine.begin() as db:
    db.execute(text("INSERT INTO app_settings(key,value_json,version) VALUES(:key,CAST(:value AS jsonb),1)"),
        {'key':'bilibili.tags.'+str(ai_task),'value':json.dumps({'summary':'unique-summarized-ai-source'})})
code,ai_job=api('/bilibili/publish',ai_payload)
assert code==200 and ai_job['state']=='submitting',(code,ai_job)
process(ai_task,'published')
code,ai_result=api('/bilibili/publish',ai_payload)
assert code==200 and ai_result['state']=='published',(code,ai_result)
counts=counters()
assert counts['ai']==1 and counts['type_catalog']==1 and counts['submit']==4 and counts['predict']==1,counts
print('PASS: direct ai_summary reuses saved task summary, native translation model and official partition leaf whitelist')

bad_task,bad_payload=fixture('ai-invalid')
bad_payload['typeid_mode']='ai_summary'
code,bad_job=api('/bilibili/publish',bad_payload)
assert code==200 and bad_job['state']=='submitting',(code,bad_job)
process(bad_task,'published')
counts=counters()
assert counts['ai']==2 and counts['ai_invalid']==1 and counts['type_catalog']==2 and counts['predict']==2 and counts['submit']==5,counts
print('PASS: invalid AI partition ID is rejected and original Python fallback predicts a valid Bilibili type')

# Original Python publish_batches are the only source of truth. Neither a
# missing peer platform job nor a failed peer target can become "published"
# solely because Bilibili returned a confirmed archive.
def make_batch(task,targets):
    identity=uuid4()
    with engine.begin() as db:
        db.execute(text("""INSERT INTO publish_batches(id,task_id,state,expected_targets)
            VALUES(CAST(:batch AS uuid),CAST(:task AS uuid),'active',CAST(:expected AS jsonb))"""),
            {'batch':str(identity),'task':str(task),'expected':json.dumps(targets)})
    return identity

single=[{'key':'bilibili:default','platform':'bilibili','account_id':None}]
foreign_task,foreign_payload=fixture('batch-only')
foreign_batch=make_batch(foreign_task,single)
unrelated_task,unrelated_payload=fixture('batch-only')
code,not_own=api('/bilibili/publish',dict(unrelated_payload,batch_id=str(foreign_batch)))
assert code==400,(code,not_own)
invalid_target_task,invalid_target=fixture('batch-only')
wrong_batch=make_batch(invalid_target_task,[{'key':'douyin:default','platform':'douyin'}])
code,wrong_target=api('/bilibili/publish',dict(invalid_target,batch_id=str(wrong_batch)))
assert code==400,(code,wrong_target)
multi_task,multi_payload=fixture('batch-only')
multi_batch=make_batch(multi_task,single+[{'key':'douyin:default','platform':'douyin'}])
multi_payload['batch_id']=str(multi_batch)
code,multi_queued=api('/bilibili/publish',multi_payload)
assert code==200 and multi_queued['state']=='submitting',(code,multi_queued)
with engine.connect() as db:
    for check_task in [unrelated_task,invalid_target_task]:
        assert db.execute(text('SELECT count(*) FROM publish_jobs WHERE task_id=CAST(:task AS uuid)'),{'task':str(check_task)}).scalar_one()==0
process(multi_task,'published')
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:batch AS uuid)'),{'batch':str(multi_batch)}).scalar_one()=='active'
    assert db.execute(text('SELECT status::text FROM tasks WHERE id=CAST(:task AS uuid)'),{'task':str(multi_task)}).scalar_one()=='publishing'
    assert db.execute(text("SELECT count(*) FROM outbox_events WHERE aggregate_id=:id AND event_type='publish.cleanup'"),{'id':str(multi_batch)}).scalar_one()==0
assert api('/bilibili/publish',multi_payload)[1]['job_id']==multi_queued['job_id']
print('PASS: Bilibili success in original multi-platform batch cannot publish unfinished Douyin target or schedule premature cleanup')

batch_task,batch_payload=fixture('batch-only')
batch_id=make_batch(batch_task,single)
batch_payload['batch_id']=str(batch_id)
code,batch_receipt=api('/bilibili/publish',batch_payload)
assert code==200 and batch_receipt['state']=='submitting',(code,batch_receipt)
with engine.connect() as db:
    owned=db.execute(text('SELECT batch_id FROM publish_jobs WHERE id=CAST(:id AS uuid)'),{'id':batch_receipt['job_id']}).scalar_one()
    assert str(owned)==str(batch_id),owned
process(batch_task,'published')
code,batch_result=api('/bilibili/publish',batch_payload)
assert code==200 and batch_result['job_id']==batch_receipt['job_id'] and batch_result['state']=='published',(code,batch_result)
with engine.connect() as db:
    batch_row=db.execute(text('SELECT state,finished_at FROM publish_batches WHERE id=CAST(:batch AS uuid)'),{'batch':str(batch_id)}).one()
    assert batch_row.state=='succeeded' and batch_row.finished_at is not None,batch_row
    assert db.execute(text('SELECT status::text FROM tasks WHERE id=CAST(:task AS uuid)'),{'task':str(batch_task)}).scalar_one()=='published'
counts=counters()
assert counts['submit']==7,counts
print('PASS: original single-target Bilibili publish batch is task- and target-scoped and settled atomically with Python publish_jobs')
# A later batch does not rewrite the previous confirmed job or invalidate an
# idempotent replay under the original batch ID.
new_batch_id=make_batch(batch_task,single)
code,original_again=api('/bilibili/publish',batch_payload)
assert code==200 and original_again==batch_result,(code,original_again)
code,conflicting_reuse=api('/bilibili/publish',dict(batch_payload,batch_id=str(new_batch_id)))
assert code==409,(code,conflicting_reuse)
print('PASS: an old confirmed Bilibili batch stays idempotent even when a new batch is registered; cross-batch receipt reassignment is denied')

bad_batch_task,bad_batch_payload=fixture('batch-unknown')
unknown_batch=make_batch(bad_batch_task,single)
bad_batch_payload['batch_id']=str(unknown_batch)
code,bad_receipt=api('/bilibili/publish',bad_batch_payload)
assert code==200 and bad_receipt['state']=='submitting',(code,bad_receipt)
process(bad_batch_task,'unknown')
code,bad_final=api('/bilibili/publish',bad_batch_payload)
assert code==200 and bad_final['state']=='unknown' and bad_final['job_id']==bad_receipt['job_id'],(code,bad_final)
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:batch AS uuid)'),{'batch':str(unknown_batch)}).scalar_one()=='failed'
    status=db.execute(text('SELECT status::text,error_code FROM tasks WHERE id=CAST(:task AS uuid)'),{'task':str(bad_batch_task)}).one()
    assert status.status=='failed' and status.error_code=='PUBLISH_FAILED',status
counts=counters()
assert counts['submit']==8 and counts['ambiguous_submit']==2,counts
print('PASS: unknown Bilibili single-target batch quarantines original batch state, original job and task without resubmission')
# Fully confirmed / partially failed peer platforms are read from the original
# PublishJob table, never inferred from Bilibili's own archive result.
def other_platform(task,batch,state):
    with engine.begin() as db:
        db.execute(text("""INSERT INTO publish_jobs(id,task_id,batch_id,platform,state,meta_json)
          VALUES(CAST(:id AS uuid),CAST(:task AS uuid),CAST(:batch AS uuid),'douyin',:status,'{}'::jsonb)"""),
          {'id':str(uuid4()),'task':str(task),'batch':str(batch),'status':state})

finished_task,finished_payload=fixture('batch-only')
finished_batch=make_batch(finished_task,single+[{'platform':'douyin'}])
other_platform(finished_task,finished_batch,'published')
finished_payload['batch_id']=str(finished_batch)
code,receipt=api('/bilibili/publish',finished_payload)
assert code==200,(code,receipt)
process(finished_task,'published')
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(finished_batch)}).scalar_one()=='succeeded'
    assert db.execute(text('SELECT status::text FROM tasks WHERE id=CAST(:id AS uuid)'),{'id':str(finished_task)}).scalar_one()=='published'
    events=db.execute(text("SELECT event_type,available_at,task_name,status FROM outbox_events WHERE aggregate_id=:id ORDER BY event_type"),{'id':str(finished_batch)}).all()
    assert [e.event_type for e in events]==['publish.cleanup','workflow.publish.finished'],events
    assert [e.task_name for e in events]==['rust.publish.cleanup','rust.publish.batch_finished'],events
    assert events[0].status=='native_pending' and events[1].status in ('native_pending','native_completed'),events
    from datetime import datetime,timezone,timedelta
    assert timedelta(hours=23)<events[0].available_at-datetime.now(timezone.utc)<timedelta(hours=25),events
print('PASS: all original platform receipts reconcile a multi-target batch atomically and schedule native 24-hour cleanup')
# Native event delivery is persisted without Hatchet and correlated to
# the actual direct publication workflow. A second tick must not duplicate it.
for _ in range(2):
    subprocess.run([str(worker),'publish-batch-tick'],check=True,timeout=20)
with engine.connect() as db:
    key='workflow-publish-finished:'+str(finished_batch)+':succeeded'
    assert db.execute(text("SELECT status FROM outbox_events WHERE operation_key=:k"),{'k':key}).scalar_one()=='native_completed'
    assert db.execute(text("SELECT runtime_engine,status FROM operation_inbox WHERE operation_key=:k"),{'k':key}).one()==('rust-publish-batch','completed')
    assert db.execute(text("SELECT count(*) FROM workflow_events WHERE kind='publish.batch.finished' AND details->>'operation_key'=:k"),{'k':key}).scalar_one()==1
print('PASS: native batch completion notification, durable acknowledgment and replay dedupe')


partial_task,partial_payload=fixture('batch-only')
partial_batch=make_batch(partial_task,single+[{'platform':'douyin'}])
other_platform(partial_task,partial_batch,'failed')
partial_payload['batch_id']=str(partial_batch)
code,_=api('/bilibili/publish',partial_payload)
assert code==200,code
process(partial_task,'published')
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(partial_batch)}).scalar_one()=='partial_failed'
    task_state=db.execute(text('SELECT status::text,error_code FROM tasks WHERE id=CAST(:id AS uuid)'),{'id':str(partial_task)}).one()
    assert task_state.status=='publishing' and task_state.error_code=='PUBLISH_PARTIAL_FAILURE',task_state
    assert db.execute(text("SELECT count(*) FROM outbox_events WHERE aggregate_id=:id AND event_type='publish.cleanup'"),{'id':str(partial_batch)}).scalar_one()==0
print('PASS: one confirmed Bilibili target plus a failed Douyin target stays partial_failed without cleanup')

mixed_task,mixed_payload=fixture('batch-unknown')
mixed_batch=make_batch(mixed_task,single+[{'platform':'douyin'}])
other_platform(mixed_task,mixed_batch,'published')
mixed_payload['batch_id']=str(mixed_batch)
code,_=api('/bilibili/publish',mixed_payload)
assert code==200,code
process(mixed_task,'unknown')
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(mixed_batch)}).scalar_one()=='partial_failed'
    task_state=db.execute(text('SELECT status::text,error_code FROM tasks WHERE id=CAST(:id AS uuid)'),{'id':str(mixed_task)}).one()
    assert task_state.status=='publishing' and task_state.error_code=='PUBLISH_PARTIAL_FAILURE',task_state
print('PASS: ambiguous Bilibili result plus another platform success becomes partial_failed without hiding unknown receipt')

counts=counters()
assert counts['submit']==11 and counts['ambiguous_submit']==3,counts


# Simulate a genuine crashed Rust Worker holding an expired publish lease.
# Quarantine must check the old PostgreSQL job and batch *before* claims,
# without ever generating another external Bilibili upload or archive request.
lease_task,lease_payload=fixture('batch-only')
lease_batch=make_batch(lease_task,single+[{'platform':'douyin'}])
other_platform(lease_task,lease_batch,'published')
lease_payload['batch_id']=str(lease_batch)
code,lease_response=api('/bilibili/publish',lease_payload)
assert code==200 and lease_response['state']=='submitting',(code,lease_response)
with engine.begin() as db:
    lease_uuid=uuid4()
    affected=db.execute(text("""UPDATE operation_inbox
       SET status='running',lease_token=CAST(:lease AS uuid),
           lease_until=now()-interval '1 minute'
       WHERE task_id=CAST(:task AS uuid) AND stage='publish' AND runtime_engine='rust-v2'
       RETURNING id"""),{'lease':str(lease_uuid),'task':str(lease_task)}).all()
    assert len(affected)==1,affected
before_lease=counters()
process(lease_task,'unknown')
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(lease_batch)}).scalar_one()=='partial_failed'
    assert db.execute(text("SELECT status FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='publish' AND runtime_engine='rust-v2'"),{'id':str(lease_task)}).scalar_one()=='unknown'
    check_state=db.execute(text('SELECT status::text,error_code FROM tasks WHERE id=CAST(:id AS uuid)'),{'id':str(lease_task)}).one()
    assert check_state.status=='publishing' and check_state.error_code=='PUBLISH_PARTIAL_FAILURE',check_state
assert counters()['submit']==before_lease['submit'],(before_lease,counters())
print('PASS: expired Rust publishing lease is quarantined under Task->Workflow->Job->Batch lock order, without any UPOS/upload replay')

# Parallel independent Python V1 channel batches are not the same as a
# multi-target batch. The default Bilibili batch may finish, but the task must
# remain publishing until the separately active Douyin batch is complete.
parallel_task,parallel_payload=fixture('batch-only')
other_batch=make_batch(parallel_task,[{'platform':'douyin'}])
with engine.begin() as db:
    db.execute(text("UPDATE tasks SET status='publishing'::task_status WHERE id=CAST(:id AS uuid)"),{'id':str(parallel_task)})
other_platform(parallel_task,other_batch,'submitting')
# Historical markers with empty target lists are accepted by Python's current
# batch aggregation and must be ignored by the Rust read path.
marker=make_batch(parallel_task,[])
with engine.begin() as db:
    db.execute(text("UPDATE publish_batches SET state='finished' WHERE id=CAST(:id AS uuid)"),{'id':str(marker)})
separate_bili_batch=make_batch(parallel_task,single)
parallel_payload['batch_id']=str(separate_bili_batch)
code,parallel_queued=api('/bilibili/publish',parallel_payload)
assert code==200 and parallel_queued['state']=='submitting',(code,parallel_queued)
process(parallel_task,'published')
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(separate_bili_batch)}).scalar_one()=='succeeded'
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(other_batch)}).scalar_one()=='active'
    assert db.execute(text('SELECT status::text FROM tasks WHERE id=CAST(:id AS uuid)'),{'id':str(parallel_task)}).scalar_one()=='publishing'
    assert db.execute(text("SELECT count(*) FROM outbox_events WHERE aggregate_id=:id AND event_type='publish.cleanup'"),{'id':str(separate_bili_batch)}).scalar_one()==0
print('PASS: independent Bilibili and Douyin batches coexist; legacy empty marker ignored; active Douyin target prevents premature task published and cleanup')
counts=counters()

name_a=uuid4()
named_task,named_payload=fixture('named-account-A')
with engine.begin() as db:
    db.execute(text("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active) VALUES(CAST(:id AS uuid),'bilibili','test-a',:secret,true)"),
        {'id':str(name_a),'secret':'dbplain:v1:'+json.dumps({'cookie':'SESSDATA=isolated-account-A; bili_jct=ABCD123456789012'})})
named_payload['account_id']=str(name_a)
code,named_receipt=api('/bilibili/publish',named_payload)
assert code==200 and named_receipt['state']=='submitting',(code,named_receipt)
process(named_task,'published',str(name_a))
assert api('/bilibili/publish',named_payload)[1]['job_id']==named_receipt['job_id']
print('PASS: named Bilibili account direct post uses original accounts Cookie instead of global Cookie')

name_b=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active) VALUES(CAST(:id AS uuid),'bilibili','test-b',:secret,true)"),
        {'id':str(name_b),'secret':'dbplain:v1:'+json.dumps({'cookie':'SESSDATA=isolated-account-B; bili_jct=BCDE123456789012'})})
pair_task,first=fixture('named-account-A')
second=dict(first)
second['meta']={**first['meta'],'title':'named-account-B'}
pair_batch=make_batch(pair_task,[{'platform':'bilibili','account_id':str(name_a)},
    {'platform':'bilibili','account_id':str(name_b)}])
first.update(account_id=str(name_a),batch_id=str(pair_batch))
second.update(account_id=str(name_b),batch_id=str(pair_batch))
code,first_job=api('/bilibili/publish',first)
assert code==200 and first_job['state']=='submitting',(code,first_job)
process(pair_task,'published',str(name_a))
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(pair_batch)}).scalar_one()=='active'
    assert db.execute(text('SELECT status::text FROM tasks WHERE id=CAST(:id AS uuid)'),{'id':str(pair_task)}).scalar_one()=='publishing'
code,second_job=api('/bilibili/publish',second)
assert code==200 and second_job['job_id']!=first_job['job_id'],(code,second_job)
process(pair_task,'published',str(name_b))
with engine.connect() as db:
    assert db.execute(text('SELECT state FROM publish_batches WHERE id=CAST(:id AS uuid)'),{'id':str(pair_batch)}).scalar_one()=='succeeded'
    assert db.execute(text('SELECT count(*) FROM publish_jobs WHERE task_id=CAST(:id AS uuid)'),{'id':str(pair_task)}).scalar_one()==2
assert api('/bilibili/publish',first)[1]['job_id']==first_job['job_id']
assert api('/bilibili/publish',second)[1]['job_id']==second_job['job_id']
print('PASS: same Task publishes to two distinct Bilibili accounts in one batch, with separate Cookies and independent receipts')

code,listed=api('/bilibili/accounts')
assert code==200 and {str(name_a),str(name_b)}.issubset({x['id'] for x in listed['accounts']}),(code,listed)
assert 'SESSDATA' not in json.dumps(listed) and 'cookie_enc' not in json.dumps(listed)
code,created=api('/bilibili/accounts',{'name':'created-by-api','cookie':'SESSDATA=isolated-account-B; bili_jct=BCDE123456789012'})
assert code==200 and created['cookie_set'] and created['is_active'],(code,created)
code,reused=api('/bilibili/accounts',{'name':'created-by-api','cookie':'SESSDATA=isolated-account-B; bili_jct=BCDE123456789012'})
assert code==200 and reused['id']==created['id'],(code,reused)
code,not_valid=api('/bilibili/accounts',{'name':'invalid name','cookie':'SESSDATA=isolated-account-B; bili_jct=BCDE123456789012'})
assert code==400,(code,not_valid)
assert 'SESSDATA' not in json.dumps(created)
code,verified=api('/bilibili/accounts/'+created['id']+'/check',{})
assert code==200 and verified['check_state']=='valid' and verified['mid']==61002,(code,verified)
code,checked_list=api('/bilibili/accounts')
assert code==200 and next(v for v in checked_list['accounts'] if v['id']==created['id'])['check_state']=='valid'
print('PASS: named account profile check uses the account-specific Cookie and confirms remote Bilibili UID')
print('PASS: account-management API uses original accounts, stable upsert identity and redacted list/response')
remove=Request('http://127.0.0.1:18080/api/v1/bilibili/accounts/'+created['id'],headers=headers,method='DELETE')
with urlopen(remove,timeout=15) as reply:
    result=json.loads(reply.read())
assert not result['is_active'] and not result['cookie_set'] and not result['in_flight'],result
print('PASS: account-management disable endpoint returns no account credentials')
with engine.connect() as db:
    stored=db.execute(text('SELECT is_active,secrets_encrypted FROM accounts WHERE id=CAST(:id AS uuid)'),{'id':created['id']}).one()
    assert not stored.is_active and not stored.secrets_encrypted
code,restored=api('/bilibili/accounts',{'name':'created-by-api','cookie':'SESSDATA=isolated-account-B; bili_jct=BCDE123456789012'})
assert code==200 and restored['id']==created['id'] and restored['is_active'],(code,restored)
print('PASS: original account row supports safe revocation and reimport with stable id')


rot_task,rot_payload=fixture('named-account-A')
rot_payload['account_id']=str(name_a)
code,rot_job=api('/bilibili/publish',rot_payload)
assert code==200 and rot_job['state']=='submitting',(code,rot_job)
with engine.begin() as db:
    db.execute(text('UPDATE accounts SET secrets_encrypted=:value WHERE id=CAST(:id AS uuid)'),
      {'id':str(name_a),'value':'dbplain:v1:'+json.dumps({'cookie':'SESSDATA=isolated-rotated; bili_jct=ABCD123456789012'})})
before_rot=counters()
process(rot_task,'unknown',str(name_a))
assert api('/bilibili/publish',rot_payload)[1]['state']=='unknown'
assert counters()['submit']==before_rot['submit'] and counters()['preupload']==before_rot['preupload']
print('PASS: named Bilibili Cookie changed after enqueue cannot submit an archive')
with engine.begin() as db:
    db.execute(text('UPDATE accounts SET is_active=false WHERE id=CAST(:id AS uuid)'),{'id':str(name_b)})
wrong_task,wrong_payload=fixture('named-account-B')
code,_=api('/bilibili/publish',dict(wrong_payload,account_id=str(name_b)))
assert code==409,code
wrong_id=uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active) VALUES(CAST(:id AS uuid),'douyin','test-wrong',:secret,true)"),
       {'id':str(wrong_id),'secret':'dbplain:v1:'+json.dumps({'cookie':'SESSDATA=isolated-account-B; bili_jct=BCDE123456789012'})})
code,_=api('/bilibili/publish',dict(wrong_payload,account_id=str(wrong_id)))
assert code==409,code
with engine.connect() as db:
    assert db.execute(text('SELECT count(*) FROM publish_jobs WHERE task_id=CAST(:id AS uuid)'),{'id':str(wrong_task)}).scalar_one()==0
print('PASS: inactive Bilibili and non-Bilibili Account rows cannot publish')

counts=counters()

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

# The TaskDetail action must enter the same native publisher and original
# single-target batch, not synthesize a second workflow-only job identity.
from concurrent.futures import ThreadPoolExecutor

def task_action(task,payload):
    return api(f'/tasks/{task}/actions/publish',payload)
def batch_evidence(task,job,account=None,expected_state='active'):
    with engine.connect() as db:
        row=db.execute(text("""SELECT p.id::text,p.state::text,p.account_id::text,b.id::text AS batch_id,
             b.state AS batch_state,b.expected_targets,t.active_publish_batch_id::text AS active,
             (SELECT count(*) FROM publish_jobs WHERE task_id=t.id) AS job_count,
             (SELECT count(*) FROM publish_batches WHERE task_id=t.id) AS batch_count
             FROM tasks t JOIN publish_jobs p ON p.task_id=t.id JOIN publish_batches b ON b.id=p.batch_id
             WHERE t.id=CAST(:task AS uuid)"""),{'task':str(task)}).one()
        assert row.id==job and row.account_id==account and row.batch_id==row.active,row
        assert row.batch_state==expected_state and row.job_count==row.batch_count==1,row
        assert row.expected_targets==[{'key':'bilibili:'+(account or 'default'),'platform':'bilibili','account_id':account}],row
        return row.batch_id

# Default video selection, metadata draft, concurrency, persisted original
# job identifier and actual loopback publication receipt.
task,payload=fixture('batch-only')
body={'meta':payload['meta']}
path=f'/tasks/{task}/actions/publish'
req=Request('http://127.0.0.1:18080/api/v1'+path,data=json.dumps(body).encode(),method='POST',headers={'content-type':'application/json'})
try:urlopen(req,timeout=10);raise AssertionError('anonymous publication accepted')
except HTTPError as err:assert err.code==401
before=counters()
with ThreadPoolExecutor(max_workers=3) as pool:
    results=list(pool.map(lambda _:task_action(task,body),range(3)))
code,result=results[0]
assert code==200 and result['state']=='submitting' and result['platform']=='bilibili' and all(v==results[0] for v in results),results
batch=batch_evidence(task,result['job_id'])
assert counters()['submit']==before['submit']
process(task,'published')
code,final=task_action(task,body)
assert code==200 and final['state']=='published' and final['job_id']==result['job_id'] and final['bvid']=='BV1RUSTMOCK1',final
assert batch_evidence(task,result['job_id'],expected_state='succeeded')==batch
assert counters()['submit']==before['submit']+1
assert api('/bilibili/publish',payload)[0]==409 # never reassign to a different batch
print('PASS: TaskDetail default Bilibili action concurrent enqueue, one original batch/job, real mock receipt and replay')

# Explicit named account, video and cover. Reuse the credential fixture
# restored above; the real mock asserts the expected Cookie/CSRF.
task,payload=fixture('named-account-B')
key=payload['video']['key']; ck=f'v2/cover/{task}/cover.png'
(root/ck).parent.mkdir(parents=True,exist_ok=True);(root/ck).write_bytes(cover_file.read_bytes())
with engine.begin() as db:
    db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:a AS uuid),CAST(:t AS uuid),'cover_image',:k)"),{'a':str(uuid4()),'t':str(task),'k':ck})
body={'platform':'bilibili','account_id':restored['id'],'video_key':key,'cover_key':ck,'meta':payload['meta'],'platform_options':{'bilibili':{'typeid_mode':'meta'}}}
for bad in ({**body,'video_key':two['video']['key']},{**body,'cover_key':cover_key},{**body,'account_id':str(wrong_id)},{**body,'force_retry':True},{**body,'skip_review':True},{**body,'platform_options':{'bilibili':{'unrecognized':True}}}):
    assert task_action(task,bad)[0] in (400,409),bad
with engine.connect() as db:
    assert db.execute(text('SELECT count(*) FROM publish_batches WHERE task_id=CAST(:t AS uuid)'),{'t':str(task)}).scalar_one()==0
before=counters();code,result=task_action(task,body)
assert code==200 and result['state']=='submitting',(code,result)
batch_evidence(task,result['job_id'],restored['id']);process(task,'published',restored['id'])
assert batch_evidence(task,result['job_id'],restored['id'],'succeeded')
assert counters()['submit']==before['submit']+1 and counters()['cover']==before['cover']+1
print('PASS: TaskDetail Bilibili named account, owned video/cover, options and foreign inputs/review bypass rejected')

# Original P1/P2 media receipt checks are applied by the same Task action.
task,payload=fixture('two-part'); key=payload['video']['key'];dub_key=f'v2/dub/{task}/dub.mp4';srt_key=f'v2/sub/{task}/subtitle.srt'
(root/dub_key).parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/key,root/dub_key)
(root/srt_key).parent.mkdir(parents=True,exist_ok=True);(root/srt_key).write_text('1\n00:00:00,000 --> 00:00:01,000\n测试\n')
with engine.begin() as db:
    for kind,k in [('video_raw',key),('video_dubbed',dub_key),('subtitle_srt',srt_key)]:
        db.execute(text('INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:a AS uuid),CAST(:t AS uuid),CAST(:kind AS asset_kind),:k)'),{'a':str(uuid4()),'t':str(task),'kind':kind,'k':k})
body={'video_key':key,'video_parts':[{'video':{'type':'storage','key':key},'title':'P1中文字幕版'},{'video':{'type':'storage','key':dub_key},'title':'P2中文配音版'}],'meta':payload['meta'],'typeid_mode':'meta'}
assert task_action(task,body)[0]==409
w,j=uuid4(),uuid4()
with engine.begin() as db:
    db.execute(text("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,request_json) VALUES(CAST(:w AS uuid),CAST(:t AS uuid),1,'rust-v2','videoroll.rust.pipeline','succeeded','{}'::jsonb)"),{'w':str(w),'t':str(task)})
    db.execute(text("""INSERT INTO operation_inbox(id,operation_key,status,runtime_engine,workflow_id,task_id,stage,generation,dubbing_source_video_key,dubbing_source_srt_key,dubbing_output_video_key,dubbing_model_status,dubbing_model_job_id,dubbing_run_created_at)
        VALUES(CAST(:j AS uuid),:op,'succeeded','rust-v2',CAST(:w AS uuid),CAST(:t AS uuid),'dubbing',1,:raw,:srt,:dub,'succeeded',CAST(:model AS uuid),now())"""),{'j':str(j),'op':'task-action-dub:'+str(j),'w':str(w),'t':str(task),'raw':key,'srt':srt_key,'dub':dub_key,'model':str(uuid4())})
before=counters();code,result=task_action(task,body);assert code==200 and result['state']=='submitting',(code,result)
batch_evidence(task,result['job_id']);process(task,'published');batch_evidence(task,result['job_id'],expected_state='succeeded')
assert counters()['submit']==before['submit']+1 and counters()['complete']==before['complete']+2
print('PASS: TaskDetail Bilibili P1/P2 requires original succeeded dubbing receipt and submits two ordered parts once')

# Unknown receipt stays in the original batch; task action replay does not
# issue another archive. Original same-target draft cannot silently requeue.
task,payload=fixture('ambiguous-publish');body={'meta':payload['meta']}
before=counters();code,result=task_action(task,body);assert code==200
process(task,'unknown');code,unknown=task_action(task,body)
assert code==200 and unknown['state']=='unknown' and unknown['job_id']==result['job_id']
assert counters()['submit']==before['submit']+1
batch_evidence(task,result['job_id'],expected_state='failed')
task,payload=fixture('batch-only')
with engine.begin() as db:
    db.execute(text("INSERT INTO publish_jobs(id,task_id,platform,state) VALUES(CAST(:id AS uuid),CAST(:t AS uuid),'bilibili','draft')"),{'id':str(uuid4()),'t':str(task)})
assert task_action(task,{'meta':payload['meta']})[0]==409
print('PASS: TaskDetail unknown result remains fenced and historical draft blocks new submission')


# The actual native publisher invokes both AI decisions, verifies distinct
# public/creator IDs and submits a single archive on the isolated server.
if os.environ.get("VIDEOROLL_V2_TEST_ACTIVITY")=="true":
    with engine.begin() as db:
        db.execute(text("INSERT INTO app_settings(key,value_json,version) VALUES('bilibili.publish',CAST(:value AS jsonb),1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"),{'value':json.dumps({'ai_activity_enabled':True})})
    for title,topic,mission,selected in (
        ("魔兽世界活动验收",301,901,True),("魔兽世界普通话题验收",302,None,True),
        ("魔兽世界普通回退验收",302,None,True),("魔兽世界无关拒绝验收",None,None,False),
        ("魔兽世界AI伪造验收",None,None,False),("魔兽世界发现失败验收",None,None,False),
        ("魔兽世界人工指定验收",302,None,False)):
        task,payload=fixture(title)
        payload['meta']['tags']=['魔兽世界']
        if '人工指定' in title:payload['meta'].update(topic_id=302,activity_name='魔兽世界普通话题')
        before=counters()
        code,result=api('/bilibili/publish',payload)
        assert code==200 and result['state']=='submitting',(code,result)
        process(task,'published')
        with engine.connect() as db:
            receipt=db.execute(text("SELECT response_json FROM publish_jobs WHERE id=CAST(:id AS uuid)"),{'id':result['job_id']}).scalar_one()
        assert receipt['activity']['selected']==selected,receipt
        assert receipt['activity']['submitted_topic_id']==topic and receipt['activity']['submitted_mission_id']==mission,receipt
        after=counters()
        assert after['submit']==before['submit']+1 and after['complete']==before['complete']+1
        if '人工指定' in title:
            assert after['activity_shortlist']==before['activity_shortlist'] and receipt['activity']['reason']=='manual_preserved'
        else:
            assert after['activity_shortlist']==before['activity_shortlist']+1 and after['activity_list']==before['activity_list']+1
            audit=receipt['activity']
            assert audit['candidate_topic_count']==3 and audit['candidate_activity_count']==2,audit
            assert audit['candidate_count']==5 and audit['discovery_ms']>=0,audit
            if '发现失败' not in title:
                assert after['activity_final']==before['activity_final']+1
                assert audit['shortlist_refs']==['public:999','public:998','topic:302'],audit
                assert audit['topic_verified_count']==1 and audit['activity_verified_count']==1,audit
                assert 'public:998' in audit['rejected_refs'],audit
                assert audit['detail_checked_count']==2 and audit['total_ms']>=0,audit
                if '普通回退' in title:assert audit['decision_fallback']=='topic_not_activity',audit
                if '无关拒绝' in title:assert 'decision_fallback' not in audit,audit
    with engine.begin() as db:
        db.execute(text("DELETE FROM app_settings WHERE key='bilibili.publish'"))
    print('PASS: actual native publication activity discovery, two AI decisions, real creator mission mapping, ordinary topic, manual preserve, hallucination and AI outage single-submit fallback')
if os.environ.get("VIDEOROLL_V2_TEST_MIXED_PUBLISH")=="true":
    from mixed_publish_isolated import run_mixed
    run_mixed(engine,root,api,fixture,process,counters,worker)
