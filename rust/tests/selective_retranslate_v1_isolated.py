"""Real HTTP + native worker selective-retranslation test. Disposable Postgres only."""
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from sqlalchemy import create_engine, text

uri=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in uri and uri.endswith('/videoroll_test')
engine=create_engine(uri)
root=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT'])
base='http://127.0.0.1:18080/api/v1'
token=os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']

def post(path,payload):
    req=Request(base+path, data=json.dumps(payload).encode(),method='POST',
        headers={'content-type':'application/json','x-videoroll-v2-internal-token':token})
    try:response=urlopen(req,timeout=12)
    except HTTPError as error:response=error
    with response:
        data=json.loads(response.read())
        return response.status,data

task=uuid4()
source_flow=uuid4()
source_job=uuid4()
raw_key=f'v2/raw/{task}/source.mp4'
segments_key=f'v2/sub/{task}/{source_job}/segments.json'
old_srt=f'v2/sub/{task}/{source_job}/subtitle.srt'
video_file=root/raw_key
video_file.parent.mkdir(parents=True,exist_ok=True)
subprocess.run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error',
    '-f','lavfi','-i','color=c=blue:s=320x180:r=25',
    '-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
    '-t','3.5','-shortest','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',
    '-y',str(video_file)],check=True,timeout=30)
assert video_file.stat().st_size>5000
source=[{'start_ms':100,'end_ms':900,'text':'one'},
        {'start_ms':1000,'end_ms':1800,'text':'two'},
        {'start_ms':2000,'end_ms':2900,'text':'three'}]
translated=[{**row,'text':'旧译:'+row['text']} for row in source]
old_metadata={'source':source,'translated':translated,'translation':True,'model':'test'}
seg_file=root/segments_key
seg_file.parent.mkdir(parents=True,exist_ok=True)
seg_file.write_text(json.dumps(old_metadata,ensure_ascii=False))
old_file=root/old_srt
old_file.write_text('1\n00:00:00,100 --> 00:00:00,900\n旧译:one\n\n2\n00:00:01,000 --> 00:00:01,800\n旧译:two\n\n3\n00:00:02,000 --> 00:00:02,900\n旧译:three\n\n')
source_config={'subtitle':{'translate_enabled':True,'translate_provider':'openai',
    'translate_model_provider_id':'legacy','translate_model_id':'legacy-mock-translator',
    'target_lang':'zh','translate_style':'口语自然'},
    'render':{'burn_in':True,'video_codec':'h264','use_intel_gpu':False},
    'auto_publish':False,'auto_publish_platforms':[]}
provider_config={'default_provider':'openai','openai':{
    'base_url':'http://127.0.0.1:18082/v1',
    'model':'legacy-mock-translator','api_key_enc':'dbplain:v1:isolated-translation-test'}}
with engine.begin() as db:
    db.execute(text("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,runtime_origin) VALUES(CAST(:id AS uuid),'local','own','subtitle_ready','rust','upload_manual')"),{'id':str(task)})
    db.execute(text("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(CAST(:flow AS uuid),CAST(:task AS uuid),1,'succeeded','cleanup',CAST(:cfg AS jsonb))"),
        {'flow':str(source_flow),'task':str(task),'cfg':json.dumps(source_config)})
    db.execute(text("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,result) VALUES(CAST(:job AS uuid),CAST(:flow AS uuid),CAST(:task AS uuid),'subtitle',1,:key,'succeeded','{}'::jsonb)"),
        {'job':str(source_job),'flow':str(source_flow),'task':str(task),'key':f'source:test:{source_job}'})
    for kind,key in [('video_raw',raw_key),('video_final',raw_key),('segments_json',segments_key),('subtitle_srt',old_srt)]:
        db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:id AS uuid),CAST(:task AS uuid),CAST(:kind AS asset_kind),:key)"),
            {'id':str(uuid4()),'task':str(task),'kind':kind,'key':key})
    db.execute(text("INSERT INTO app_settings(key,value_json,version) VALUES('subtitle.translate',CAST(:cfg AS jsonb),1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"),
        {'cfg':json.dumps(provider_config)})
    db.execute(text("INSERT INTO app_settings(key,value_json,version) VALUES('subtitle.auto_profile',CAST(:cfg AS jsonb),1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"),
        {'cfg':json.dumps({'translate_enabled':True,'translate_provider':'openai','translate_model_provider_id':'legacy','translate_model_id':'legacy-mock-translator','auto_publish':False})})
for invalid in [[],[999],[0],list(range(1,501))+[501]]:
    code,body=post(f'/tasks/{task}/actions/subtitle_retranslate',{'indices':invalid})
    assert code in (400,422),(code,body)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM vr2_jobs WHERE task_id=CAST(:id AS uuid) AND generation>1"),{'id':str(task)}).scalar()==0
code,response=post(f'/tasks/{task}/actions/subtitle_retranslate',{'indices':[3,1,3]})
assert code==200 and response['status']=='queued',(code,response)
job_id=response['job_id']
code,same=post(f'/tasks/{task}/actions/subtitle_retranslate',{'indices':[1,3]})
assert code==200 and same==response,(code,same)
code,conflict=post(f'/tasks/{task}/actions/subtitle_retranslate',{'indices':[2]})
assert code==409,(code,conflict)
with engine.connect() as db:
    entry=db.execute(text("SELECT status,operation_key FROM operation_inbox WHERE task_id=CAST(:id AS uuid) AND stage='subtitle' AND generation=2"),{'id':str(task)}).one()
    assert entry.status=='pending' and entry.operation_key.startswith('vr2:'),entry
print('PASS: selective action accepted real pinned subtitle stage, deduped indices, rejected competing/invalid actions')

# Worker startup is independent of the ASR endpoint; the selective branch must
# use the preserved snapshot and local mock translator, never the ASR gateway.
proc=subprocess.Popen([str(Path(__file__).resolve().parents[1]/'target/debug/videoroll-v2'),'worker'],
    env={**os.environ,'VIDEOROLL_V2_WORKERS':'1','RUST_LOG':'warn'},
    stdout=(Path(__file__).resolve().parents[1]/'target/selective-worker.log').open('w'),
    stderr=subprocess.STDOUT)
try:
    deadline=time.monotonic()+90
    final_state=None
    while time.monotonic()<deadline:
        with engine.connect() as db:
            row=db.execute(text("SELECT state,last_error FROM vr2_workflows WHERE task_id=CAST(:task AS uuid) AND generation=2"),{'task':str(task)}).one()
            final_state=row.state
            if final_state in ('succeeded','failed','unknown'):
                if final_state!='succeeded':raise AssertionError((final_state,row.last_error))
                break
        if proc.poll() is not None:raise AssertionError('isolated Rust worker unexpectedly exited')
        time.sleep(.5)
    assert final_state=='succeeded',f'selective workflow timed out: {final_state}'
finally:
    proc.terminate()
    try:proc.wait(timeout=8)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
with engine.connect() as db:
    assets=db.execute(text("SELECT kind::text,storage_key FROM assets WHERE task_id=CAST(:id AS uuid) ORDER BY created_at DESC,id DESC"),{'id':str(task)}).all()
    jobs=db.execute(text("SELECT stage,state,result FROM vr2_jobs WHERE task_id=CAST(:id AS uuid) AND generation=2 ORDER BY created_at,id"),{'id':str(task)}).all()
assert jobs[0].stage=='subtitle' and jobs[0].state=='succeeded',jobs
assert next(job for job in jobs if job.stage=='publish').result.get('skipped'),jobs
asset_map={}
for kind,key in assets:asset_map.setdefault(kind,key)
new_segments=asset_map['segments_json']; new_srt=asset_map['subtitle_srt'];new_ass=asset_map['subtitle_ass'];new_video=asset_map['video_final']
assert new_segments!=segments_key and new_srt!=old_srt
assert new_video!=raw_key and (root/new_video).stat().st_size>5000,new_video
new_payload=json.loads((root/new_segments).read_text())
assert new_payload['source']==source,new_payload
actual=new_payload['translated']
assert actual[0]['text']=='译文：one' and actual[1]['text']=='旧译:two' and actual[2]['text']=='译文：three',actual
assert [x['start_ms'] for x in actual]==[100,1000,2000]
assert '旧译:two' in (root/new_srt).read_text()
assert '译文：one' in (root/new_srt).read_text()
assert (root/new_ass).stat().st_size>50
assert (root/segments_key).read_text()==json.dumps(old_metadata,ensure_ascii=False)
print('PASS: Rust worker selectively retranslated only chosen cues, preserved timeline/source and old assets, versioned SRT/ASS and re-rendered new video without publication')

# A separate task verifies native Index-Translate's selected-window path; the
# OpenAI mock test above would not exercise Index's numbered response adapter.
index_task=uuid4()
index_flow=uuid4()
index_source_job=uuid4()
index_video=f'v2/raw/{index_task}/source.mp4'
index_segments=f'v2/sub/{index_task}/{index_source_job}/segments.json'
index_srt=f'v2/sub/{index_task}/{index_source_job}/subtitle.srt'
(root/index_video).parent.mkdir(parents=True,exist_ok=True)
import shutil
shutil.copyfile(video_file,root/index_video)
(root/index_segments).parent.mkdir(parents=True,exist_ok=True)
(root/index_segments).write_text(json.dumps(old_metadata,ensure_ascii=False))
(root/index_srt).write_text(old_file.read_text())
index_config=json.loads(json.dumps(source_config))
index_config['subtitle'].update({'translate_provider':'index_translate',
    'translate_model_provider_id':'index_translate',
    'translate_model_id':'Index-Translate-35B-A3B'})
index_config['render']['burn_in']=False
with engine.begin() as db:
    db.execute(text("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,runtime_origin) VALUES(CAST(:id AS uuid),'local','own','subtitle_ready','rust','upload_manual')"),{'id':str(index_task)})
    db.execute(text("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(CAST(:flow AS uuid),CAST(:task AS uuid),1,'succeeded','cleanup',CAST(:cfg AS jsonb))"),
        {'flow':str(index_flow),'task':str(index_task),'cfg':json.dumps(index_config)})
    db.execute(text("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,result) VALUES(CAST(:job AS uuid),CAST(:flow AS uuid),CAST(:task AS uuid),'subtitle',1,:key,'succeeded','{}'::jsonb)"),
        {'job':str(index_source_job),'flow':str(index_flow),'task':str(index_task),'key':f'source:test:{index_source_job}'})
    for kind,key in [('video_raw',index_video),('segments_json',index_segments),('subtitle_srt',index_srt)]:
        db.execute(text("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:id AS uuid),CAST(:task AS uuid),CAST(:kind AS asset_kind),:key)"),
            {'id':str(uuid4()),'task':str(index_task),'kind':kind,'key':key})
code,queued=post(f'/tasks/{index_task}/actions/subtitle_retranslate',{'indices':[2]})
assert code==200 and queued['status']=='queued',(code,queued)
proc=subprocess.Popen([str(Path(__file__).resolve().parents[1]/'target/debug/videoroll-v2'),'worker'],
    env={**os.environ,'VIDEOROLL_V2_WORKERS':'1','RUST_LOG':'warn'},
    stdout=(Path(__file__).resolve().parents[1]/'target/selective-index-worker.log').open('w'),
    stderr=subprocess.STDOUT)
try:
    deadline=time.monotonic()+90
    done=False
    while time.monotonic()<deadline:
        with engine.connect() as db:
            row=db.execute(text("SELECT state,last_error FROM vr2_workflows WHERE task_id=CAST(:task AS uuid) AND generation=2"),{'task':str(index_task)}).one()
            if row.state in ('succeeded','failed','unknown'):
                assert row.state=='succeeded',(row.state,row.last_error)
                done=True
                break
        if proc.poll() is not None:raise AssertionError('Index worker exited unexpectedly')
        time.sleep(.5)
    assert done,'Index retranslation did not finish'
finally:
    proc.terminate()
    try:proc.wait(timeout=8)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
with engine.connect() as db:
    new_index=db.execute(text("SELECT storage_key FROM assets WHERE task_id=CAST(:task AS uuid) AND kind='segments_json'::asset_kind ORDER BY created_at DESC,id DESC LIMIT 1"),{'task':str(index_task)}).scalar_one()
assert new_index!=index_segments
index_out=json.loads((root/new_index).read_text())['translated']
assert [row['text'] for row in index_out]==['旧译:one','译文：two','旧译:three'],index_out
print('PASS: selected-only Index-Translate used a real loopback numbered translation API, preserving other cues and producing independent subtitle revision')
