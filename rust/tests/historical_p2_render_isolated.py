"""Original historical audio-only P2 requeue -> real Rust/FFmpeg settlement."""
import concurrent.futures, hashlib, json, os, socket, subprocess, time, uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import psycopg

url=os.environ['DATABASE_URL']
assert '127.0.0.1:' in url and url.endswith('/videoroll_test')
root=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT'])
token=os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']
assert token.startswith('isolated-')
exe=Path(__file__).resolve().parents[1]/'target/debug/videoroll-v2'
env={**os.environ,'VIDEOROLL_V2_NATIVE_OUTBOX_ONLY':'true','VIDEOROLL_V2_RENDER_MANAGEMENT_NATIVE':'true','VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL':'false','VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH':'false','VIDEOROLL_V2_COMPAT_WRITE':'false','VIDEOROLL_V2_SOURCE_SCANNER':'false','VIDEOROLL_V2_REMOTE_RENDER':'false'}
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
base=f'http://127.0.0.1:{port}'
mgmt='/api/v1/render-management/executions/'
def db():return psycopg.connect(url)
def call(method,path,body=None,auth=True):
    headers={'Content-Type':'application/json'}
    if auth:headers['x-videoroll-v2-internal-token']=token
    try:r=urlopen(Request(base+path,method=method,data=None if body is None else json.dumps(body).encode(),headers=headers),timeout=10)
    except HTTPError as e:r=e
    with r:return r.status,json.load(r)
def tick():subprocess.run([str(exe),'historical-p2-render-tick'],env=env,check=True,timeout=45)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
video=root/'historical-fixture.mp4';audio=root/'historical-fixture.m4a'
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=128x96:d=1','-c:v','mpeg4','-y',str(video)],check=True,timeout=30)
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','aac','-y',str(audio)],check=True,timeout=30)
def seed(*,owner='rust',wrong=False,live=False,oldflow=False,corrupt=False):
    t,r,d,e,w,p=[uuid.uuid4() for _ in range(6)]
    vk=f'raw/{t}/source.mp4';ak=f'dub/{t}/audio/mixed-{sha(audio)}.m4a'
    srt=b'1\n00:00:00,000 --> 00:00:00,800\nhello\n\n'
    sk=f'sub/{t}/dubbed-{hashlib.sha256(srt).hexdigest()}.srt'
    for key,data in [(vk,video.read_bytes()),(ak,audio.read_bytes()),(sk,srt+(b'bad' if corrupt else b''))]:
        path=root/key;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    req={'dubbing_job_id':str(d),'input_key':vk,'audio_key':ak,'srt_key':sk,'burn_in':True,'runtime_profile':False,'render':{'video_codec':'h264','video_preset':'fast','video_crf':23,'ass_style':'clean_white','primary_font_scale_percent':100,'secondary_font_scale_percent':100}}
    out={'render_job_id':str(uuid.uuid4() if wrong else r),'source_video_key':vk,'audio_key':ak,'aligned_srt_key':sk,'youdub':{'task_id':str(d),'stage':'rendering_p2','stages':['mixed_audio']}}
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','published',%s)",(t,owner))
        c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,aid,bvid,response_json) VALUES(%s,%s,'bilibili','published','123','BVfixture','{}')",(p,t))
        c.execute("INSERT INTO render_workers(id,worker_key,name,platform) VALUES(%s,%s,'old worker','linux')",(w,str(w)))
        c.execute("INSERT INTO dubbing_jobs(id,task_id,status,output_json) VALUES(%s,%s,'failed',%s)",(d,t,json.dumps(out)))
        c.execute("INSERT INTO render_jobs(id,task_id,status,operation_key,request_json) VALUES(%s,%s,'failed',%s,%s)",(r,t,f'youdub:p2-render:{d}',json.dumps(req)))
        c.execute("INSERT INTO render_executions(id,render_job_id,worker_id,state,lease_until) VALUES(%s,%s,%s,'failed',CASE WHEN %s THEN now()+interval '3 minutes' ELSE NULL END)",(e,r,w,live))
        for key,kind in [(vk,'video_raw'),(ak,'dubbing_audio')]:
            path=root/key;c.execute('INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s,%s,%s,%s)',(uuid.uuid4(),t,kind,key,sha(path),path.stat().st_size))
        if oldflow:c.execute("INSERT INTO pipeline_runs(id,task_id,engine,workflow_name,state) VALUES(%s,%s,'hatchet','fixture','running')",(uuid.uuid4(),t))
    return t,r,d,e,p

def requeue(item,auth=True):return call('POST',mgmt+str(item[3])+'/requeue',{'reason':'isolated P2 recovery'},auth)
def verify(item,expected):
    t,r,d,e,p=item
    with db() as c:
        row=c.execute("SELECT status,attempt_count,result_json,last_error,lease_owner FROM operation_inbox WHERE operation_key=%s",(f'rust-historical-p2-render:{r}',)).fetchone()
        assert row is not None and row[0]==expected and row[4] is None,row
        status='succeeded' if expected=='completed' else 'failed'
        assert c.execute('SELECT status::text,lease_owner FROM render_jobs WHERE id=%s',(r,)).fetchone()==(status,None)
        state,out,owner=c.execute('SELECT status,output_json,lease_owner FROM dubbing_jobs WHERE id=%s',(d,)).fetchone()
        assert state==status and owner is None
        assert c.execute('SELECT state FROM render_executions WHERE id=%s',(e,)).fetchone()[0]=='failed'
        assert c.execute('SELECT status::text FROM tasks WHERE id=%s',(t,)).fetchone()[0]=='published'
        assert c.execute('SELECT state::text,aid,bvid,response_json FROM publish_jobs WHERE id=%s',(p,)).fetchone()==('published','123','BVfixture',{})
        assert c.execute('SELECT count(*) FROM publish_jobs WHERE task_id=%s',(t,)).fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM pipeline_runs WHERE task_id=%s',(t,)).fetchone()[0]==0
        assets=c.execute("SELECT id,storage_key,sha256,size_bytes FROM assets WHERE task_id=%s AND kind::text='video_dubbed'",(t,)).fetchall()
        if expected=='completed':
            assert len(assets)==1 and row[1]==1 and row[2]['historical_dubbing_id']==str(d)
            aid,key,digest,size=assets[0];path=root/key
            assert row[2]['asset_id']==str(aid) and row[2]['video_key']==key and out['video_key']==key and out['youdub']['stage']=='done'
            assert digest==sha(path) and size==path.stat().st_size
            streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_type','-of','json',str(path)]))['streams']
            assert {s['codec_type'] for s in streams}=={'video','audio'}
        else:assert not assets and row[3]

proc=subprocess.Popen([str(exe),'api'],env={**env,'VIDEOROLL_V2_BIND':f'127.0.0.1:{port}'})
try:
    for _ in range(100):
        try:
            with urlopen(base+'/ready',timeout=1) as response:
                if response.status==200:break
        except (URLError,ConnectionError):time.sleep(.2)
        assert proc.poll() is None
    else:raise AssertionError('isolated API unavailable')
    good=seed()
    assert requeue(good,False)[0]==401
    code,row=call('GET',mgmt+str(good[3]));assert code==200 and row['requeue_supported'] and not row['cancel_supported'],row
    for blocked in (seed(owner='python'),seed(wrong=True),seed(live=True),seed(oldflow=True)):
        assert requeue(blocked)[0]==409
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:requeue(good),range(3)))
    assert all(code==200 and body['job_status']=='queued' and body['historical_recovery_status']=='pending' for code,body in results),results
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda _:tick(),range(3)))
    verify(good,'completed')
    assert requeue(good)[0]==200;tick();verify(good,'completed')
    print('PASS: original P2 requeue, concurrent HTTP/ticks, real video+audio render, exact historical IDs, no new workflow or publication',flush=True)
    corrupt=seed(corrupt=True);assert requeue(corrupt)[0]==200;tick();verify(corrupt,'failed');assert requeue(corrupt)[0]==409
    expired=seed();assert requeue(expired)[0]==200
    t,r,d,e,p=expired;lease=uuid.uuid4();owner=f'rust-p2:{lease}'
    with db() as c:
        c.execute("UPDATE operation_inbox SET status='running',lease_owner=%s,lease_token=%s,lease_until=now()-interval '1 second',attempt_count=1 WHERE operation_key=%s",(owner,lease,f'rust-historical-p2-render:{r}'))
        c.execute("UPDATE render_jobs SET status='running',lease_owner=%s,lease_until=now()-interval '1 second' WHERE id=%s",(owner,r))
        c.execute("UPDATE dubbing_jobs SET lease_owner=%s,lease_until=now()-interval '1 second' WHERE id=%s",(owner,d))
    tick();verify(expired,'failed');assert requeue(expired)[0]==409
    print('PASS: corrupt aligned SRT, old ownership, invalid binding, live legacy lease, active Hatchet, expired recovery lease fail closed',flush=True)
finally:
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
