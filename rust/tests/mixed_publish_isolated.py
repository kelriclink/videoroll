"""Original mixed publish_all admission and both real Rust consumers; no network side effects."""
import hashlib,json,os,subprocess,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from sqlalchemy import text
from videoroll.apps.security.service_auth import service_token
from types import SimpleNamespace

def run_mixed(engine,root,api,fixture,process,counters,worker):
    assert os.environ['DATABASE_URL'].endswith('/videoroll_test') and '127.0.0.1:' in os.environ['DATABASE_URL']
    assert os.environ['VIDEOROLL_V2_TEST_BILIBILI_BASE'].rstrip('/')=='http://127.0.0.1:18199'
    assert os.environ['VIDEOROLL_V2_NATIVE_OUTBOX_ONLY']=='true'
    def db(q,params={},all=False):
        with engine.begin() as c:
            res=c.execute(text(q),params)
            if res.returns_rows:return res.all() if all else res.one()
    db("UPDATE app_settings SET value_json=CAST(:v AS jsonb) WHERE key='publish.platforms'",{'v':json.dumps({'bilibili':True,'douyin':True,'kuaishou':True,'xiaohongshu':True})})
    def account(platform,name,valid=True):
        a=str(uuid.uuid4());db("INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active,check_state) VALUES(CAST(:a AS uuid),CAST(:p AS platform),:n,:s,true,:check)",{'a':a,'p':platform,'n':name,'s':'dbplain:v1:{"cookies":[],"origins":[]}','check':'valid' if valid else 'invalid'});return a
    good=account('douyin','mixed-ok');bad=account('kuaishou','mixed-bad',False);fail=account('kuaishou','mixed-fail')
    def seed(title='batch-only'):
        t,p=fixture(title);key=p['video']['key'];raw=(root/key).read_bytes()
        db("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES(:k,:sha,:size,'live',now()+interval '2 days')",{'k':key,'sha':hashlib.sha256(raw).hexdigest(),'size':len(raw)})
        db("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(:k,'task',CAST(:t AS uuid),'video_final')",{'k':key,'t':str(t)})
        body={'platforms':['bilibili','douyin'],'account_ids':{'douyin':good},'video_key':key,'platform_meta':{'bilibili':p['meta'],'douyin':{'title':'mixed fixture','desc':'原作者：Fixture','tags':['test']}}}
        return str(t),body
    def post(t,body):return api(f'/tasks/{t}/actions/publish_all',body)
    def verify(t,r,n):
        assert r['platform_count']==n and len(r['results'])==n
        b=r['batch_id'];rows=db("SELECT p.platform::text,p.id::text,p.state::text,p.batch_id::text FROM publish_jobs p WHERE p.task_id=CAST(:t AS uuid) ORDER BY p.platform",{'t':t},True)
        assert len(rows)==n and all(row[3]==b and row[1]==r['results'][row[0]]['job_id'] for row in rows),rows
        assert db('SELECT count(*) FROM publish_batches WHERE task_id=CAST(:t AS uuid)',{'t':t})==(1,)
        assert db('SELECT active_publish_batch_id::text FROM tasks WHERE id=CAST(:t AS uuid)',{'t':t})==(b,)
        code,history=api(f'/tasks/{t}/publish_jobs');assert code==200 and len(history)==n,(code,history)
        assert {x['id'] for x in history}=={row[1] for row in rows} and all(x['batch_id']==b for x in history),history

        return b
    def state(t,b):return db("SELECT b.state,t.status::text,t.error_code,(SELECT count(*) FROM outbox_events WHERE aggregate_id=:b AND event_type='publish.cleanup') FROM tasks t JOIN publish_batches b ON b.task_id=t.id WHERE t.id=CAST(:t AS uuid) AND b.id=CAST(:b AS uuid)",{'t':t,'b':b})
    # Fail after Bilibili's writes and after the first social target, so the
    # database must roll back every admission row, not just the last platform.
    t,body=seed();invalid=json.loads(json.dumps(body));invalid['platforms'].append('kuaishou');invalid['account_ids']['kuaishou']=bad;invalid['platform_meta']['kuaishou']={'title':'invalid fixture'}
    before=counters();code,err=post(t,invalid);assert code==409,(code,err)
    assert db("SELECT (SELECT count(*) FROM publish_batches WHERE task_id=CAST(:t AS uuid)),(SELECT count(*) FROM publish_jobs WHERE task_id=CAST(:t AS uuid)),(SELECT count(*) FROM pipeline_runs WHERE task_id=CAST(:t AS uuid)),(SELECT count(*) FROM operation_inbox WHERE task_id=CAST(:t AS uuid)),status::text,active_publish_batch_id FROM tasks WHERE id=CAST(:t AS uuid)",{'t':t})==(0,0,0,0,'rendered',None)
    assert counters()==before
    # Three-way concurrent exact admission, changed request and separate facade rejected.
    with ThreadPoolExecutor(max_workers=3) as pool:rs=list(pool.map(lambda _:post(t,body),range(3)))
    code,r=rs[0];assert code==200 and r['all_accepted'] and not r['all_published'] and all(x==rs[0] for x in rs),rs
    b=verify(t,r,2);assert state(t,b)==('active','publishing',None,0)
    assert post(t,{**body,'platforms':['bilibili','douyin','kuaishou']})[0] in (400,409)
    assert post(t,{**body,'force_retry':True})[0] in (400,409)
    assert api(f'/tasks/{t}/actions/publish',{'platform':'douyin','account_id':good,'video_key':body['video_key'],'meta':body['platform_meta']['douyin']})[0] in (409,502)
    assert db("SELECT e.status,i.runtime_engine,i.status FROM publish_jobs p JOIN outbox_events e ON e.operation_key=p.operation_key JOIN operation_inbox i ON i.operation_key=p.operation_key WHERE p.batch_id=CAST(:b AS uuid) AND p.platform='douyin'",{'b':b})==('native_pending','rust-sau-publish','pending')
    # Execute actual Bili Worker first; no premature success or cleanup.
    process(uuid.UUID(t),'published');assert state(t,b)==('active','publishing',None,0),state(t,b)
    assert counters()['submit']==before['submit']+1
    print('PASS: mixed publish_all atomic rollback, concurrent stable batch, shared original jobs and Bilibili-first active aggregation',flush=True)
    with tempfile.TemporaryDirectory(prefix='vr-mixed-sau-') as tmp:
        runtime=Path(tmp);cookies=runtime/'cookies';cookies.mkdir(mode=0o700);mock=runtime/'sau'
        mock.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
p,mode,flag,n,*args=sys.argv[1:]
assert mode=='upload-video' and flag=='--account'
assert Path(args[args.index('--file')+1]).is_file()
with open('calls','a') as f:f.write(n+'\\n')
if n=='mixed-fail':sys.exit(1)
''');mock.chmod(0o700)
        exe=worker.parent/'videoroll-sau'
        env={**os.environ,'VIDEOROLL_V2_SAU_BIND':'127.0.0.1:18192','VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK':'true','VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN':'false','VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH':'true','VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX':'false','SAU_RUNTIME_DIR':tmp,'SAU_COOKIES_DIR':str(cookies),'SAU_EXECUTABLE':str(mock),'STORAGE_ROOT':str(root),'SAU_UPLOAD_TIMEOUT_SECONDS':'5'}
        def wait(f):
            end=time.monotonic()+35
            while time.monotonic()<end:
                if f():return
                time.sleep(.15)
            raise AssertionError('mixed SAU timeout')
        def settle(t,b,expected):
            wait(lambda:db("SELECT count(*) FROM publish_jobs p JOIN outbox_events e ON e.operation_key=p.operation_key WHERE p.batch_id=CAST(:b AS uuid) AND p.platform<>'bilibili' AND e.status='native_completed'",{'b':b})==(expected,))
        proc=subprocess.Popen([str(exe)],env=env)
        try:
            settle(t,b,1);assert state(t,b)==('partial_failed','publishing','PUBLISH_PARTIAL_FAILURE',0),state(t,b)
            code,final=post(t,body);assert code==200 and final['batch_id']==b and not final['all_published'] and final['error_count']==1 and final['results']['douyin']['state']=='submitted',final
            assert post(t,body)[1]==final
            aid=db("SELECT id::text FROM assets WHERE task_id=CAST(:t AS uuid) AND kind='video_final'",{'t':t})[0]
            req=Request(f'http://127.0.0.1:18080/api/v1/tasks/{t}/assets/{aid}',method='DELETE',headers={'x-videoroll-v2-internal-token':os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']})
            try:urlopen(req,timeout=10);raise AssertionError('unverified mixed video was deleted')
            except HTTPError as err:assert err.code==409,err.code
            assert (root/body['video_key']).is_file()
            code,history=api(f'/tasks/{t}/publish_jobs');assert code==200 and next(x for x in history if x['platform']=='douyin')['state']=='unknown',history

            assert (runtime/'calls').read_text().splitlines()==['mixed-ok']
            # SAU-first ordering, with a real CLI failure after start and a
            # successful-but-unverified submission. Neither may count as success.
            t2,body2=seed();body2['platforms'].append('kuaishou');body2['account_ids']['kuaishou']=fail;body2['platform_meta']['kuaishou']={'title':'mixed failure'}
            code,r2=post(t2,body2);assert code==200,(code,r2);b2=verify(t2,r2,3)
            settle(t2,b2,2);assert state(t2,b2)==('active','publishing',None,0),state(t2,b2)
            process(uuid.UUID(t2),'published');assert state(t2,b2)==('partial_failed','publishing','PUBLISH_PARTIAL_FAILURE',0),state(t2,b2)
            code,r2final=post(t2,body2);assert code==200 and r2final['error_count']==2 and not r2final['all_published'],r2final
            assert sorted((runtime/'calls').read_text().splitlines())==['mixed-fail','mixed-ok','mixed-ok']
            # Independent native SAU API must not add a fourth target to the mixed batch.
            a=account('xiaohongshu','mixed-other');token=service_token(SimpleNamespace(internal_api_secret=os.environ['INTERNAL_API_SECRET'],development_mode=False))
            req=Request('http://127.0.0.1:18192/sau/xiaohongshu/publish',headers={'X-Videoroll-Internal-Token':token,'Content-Type':'application/json'},data=json.dumps({'platform':'xiaohongshu','task_id':t2,'account_id':a,'video':{'key':body2['video_key']},'meta':{'title':'foreign target'}}).encode(),method='POST')
            try:urlopen(req,timeout=10);raise AssertionError('separate SAU target was admitted')
            except HTTPError as err:assert err.code==409,err.code
            verify(t2,r2final,3)
        finally:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
        assert not list(cookies.glob('*.json'))
    print('PASS: mixed batch real mock SAU execution, both completion orders, unknown/unverified receipt quarantine and no premature cleanup',flush=True)
