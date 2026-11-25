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
            # Explicit creator-center attestation, never automatic proof or replay.
            def resolve(task,job,body):return api(f'/tasks/{task}/publish_jobs/{job}/sau/resolve',body)
            j=final['results']['douyin']['job_id'];j2=r2final['results']['douyin']['job_id'];k2=r2final['results']['kuaishou']['job_id']
            receipt={'platform':'douyin','account_id':good,'external_id':'7412345678901234567','evidence':'测试操作者在模拟创作者中心核对对应作品和账号','confirmed_in_creator_center':True}
            assert resolve(t,j,{**receipt,'confirmed_in_creator_center':False})[0]==400
            assert resolve(t,j,{**receipt,'external_id':'https://example.invalid/video'})[0]==400
            assert resolve(t,j,{**receipt,'account_id':fail})[0]==409
            assert resolve(t,j2,receipt)[0]==404
            assert resolve(t,final['results']['bilibili']['job_id'],receipt)[0]==409
            req=Request(f'http://127.0.0.1:18080/api/v1/tasks/{t}/publish_jobs/{j}/sau/resolve',headers={'content-type':'application/json'},data=json.dumps(receipt).encode(),method='POST')
            try:urlopen(req,timeout=10);raise AssertionError('anonymous receipt accepted')
            except HTTPError as err:assert err.code==401,err.code
            before_calls=(runtime/'calls').read_text();before=counters()
            # A previously failed batch may be old. Cleanup must use the new
            # successful settlement time, never the old failure timestamp.
            db("UPDATE publish_batches SET finished_at=now()-interval '3 days' WHERE id=CAST(:b AS uuid)",{'b':b})
            with ThreadPoolExecutor(max_workers=3) as pool:rs=list(pool.map(lambda _:resolve(t,j,receipt),range(3)))
            code,attested=rs[0];assert code==200 and all(x==rs[0] for x in rs) and attested['verification']=='operator_creator_center',rs
            assert state(t,b)==('succeeded','published',None,1),state(t,b)
            row=db("SELECT p.state::text,p.external_id,p.response_json,i.result_json,e.status FROM publish_jobs p JOIN operation_inbox i ON i.operation_key=p.operation_key JOIN outbox_events e ON e.operation_key=p.operation_key WHERE p.id=CAST(:j AS uuid)",{'j':j})
            assert row[0:2]==('published',receipt['external_id']) and row[2]['receipt_verified'] is False and row[2]['operator_receipt']['source']=='operator_creator_center' and row[2]['operator_receipt']['evidence']==receipt['evidence'] and row[3]['status']=='submitted' and row[4]=='native_completed',row
            assert db("SELECT count(*) FROM outbox_events WHERE aggregate_id=:b AND event_type='workflow.publish.finished' AND args_json->'args'->>1='succeeded'",{'b':b})==(1,)
            assert db("SELECT available_at BETWEEN now()+interval '23 hours' AND now()+interval '25 hours' FROM outbox_events WHERE aggregate_id=:b AND event_type='publish.cleanup'",{'b':b})==(True,)
            assert resolve(t,j,{**receipt,'evidence':'不同的核验说明不得覆盖原确认记录'})[0]==409
            assert resolve(t2,j2,receipt)[0]==409 # ID cannot be assigned to a second job
            # Two uncertain targets in another batch; only the final explicit
            # confirmation can settle all targets. Unknown after browser start
            # may be reconciled but no upload is ever retried.
            receipt2={**receipt,'external_id':'7412345678901234568'}
            assert resolve(t2,j2,receipt2)[0]==200
            assert state(t2,b2)==('partial_failed','publishing','PUBLISH_PARTIAL_FAILURE',0)
            kreceipt={**receipt,'platform':'kuaishou','account_id':fail,'external_id':'KuaishouFixture123','evidence':'测试操作者在模拟后台核对启动后未知的快手作品'}
            assert resolve(t2,k2,kreceipt)[0]==200
            assert state(t2,b2)==('succeeded','published',None,1)
            assert post(t2,body2)[1]['all_published'] is True
            assert (runtime/'calls').read_text()==before_calls and counters()==before
            verify(t2,post(t2,body2)[1],3)
            print('PASS: explicit operator receipt attestation, exact original evidence, concurrent dedupe, duplicate ID rejection, success settlement and fresh delayed cleanup without replay',flush=True)
            # Pure SAU uses the same original batch, not the old facade intent.
            t3,body3=seed();body3['platforms']=['douyin','kuaishou'];body3['platform_meta'].pop('bilibili');body3['platform_meta']['kuaishou']={'title':'pure SAU fixture'};body3['account_ids']['kuaishou']=bad
            assert post(t3,body3)[0]==409
            assert db("SELECT (SELECT count(*) FROM publish_batches WHERE task_id=CAST(:t AS uuid)),(SELECT count(*) FROM publish_jobs WHERE task_id=CAST(:t AS uuid)),(SELECT count(*) FROM vr2_social_publish_intents WHERE task_id=CAST(:t AS uuid)),status::text FROM tasks WHERE id=CAST(:t AS uuid)",{'t':t3})==(0,0,0,'rendered')
            ks=account('kuaishou','pure-ks');body3['account_ids']['kuaishou']=ks
            with ThreadPoolExecutor(max_workers=3) as pool:rs=list(pool.map(lambda _:post(t3,body3),range(3)))
            code,r3=rs[0];assert code==200 and r3['all_accepted'] and all(x==rs[0] for x in rs),rs
            b3=verify(t3,r3,2);settle(t3,b3,2)
            assert db('SELECT count(*) FROM vr2_social_publish_intents WHERE task_id=CAST(:t AS uuid)',{'t':t3})==(0,)
            assert db('SELECT count(*) FROM pipeline_runs WHERE task_id=CAST(:t AS uuid)',{'t':t3})==(0,)
            assert state(t3,b3)==('failed','failed','PUBLISH_FAILED',0),state(t3,b3)
            code,social=api(f'/tasks/{t3}/social_publish_jobs');assert code==200 and {v['job_id'] for v in social}=={v['job_id'] for v in r3['results'].values()} and all(v['state']=='unknown' for v in social),social
            dj=r3['results']['douyin']['job_id'];kj=r3['results']['kuaishou']['job_id']
            code,detail=api(f'/tasks/{t3}/social_publish_jobs/{dj}');assert code==200 and detail['job_id']==dj and detail['state']=='unknown',detail
            assert resolve(t3,dj,{**receipt,'external_id':'7412345678901234569'})[0]==200
            assert state(t3,b3)==('partial_failed','publishing','PUBLISH_PARTIAL_FAILURE',0),state(t3,b3)
            assert resolve(t3,kj,{**kreceipt,'account_id':ks,'external_id':'PureKuaishouFixture123'})[0]==200
            assert state(t3,b3)==('succeeded','published',None,1)
            assert post(t3,body3)[1]['all_published'] is True
            # Single-platform TaskDetail and equivalent publish_all share one
            # original batch and identical fingerprint, no facade dispatch.
            t4,body4=seed();body4['platforms']=['douyin'];body4['platform_meta'].pop('bilibili');body4['platform_options']={'douyin':{}}
            single={'platform':'douyin','account_id':good,'video_key':body4['video_key'],'meta':body4['platform_meta']['douyin'],'platform_options':{}}
            code,one=api(f'/tasks/{t4}/actions/publish',single);assert code==200 and one['batch_id'] and one['job_id'],(code,one)
            code,r4=post(t4,body4);assert code==200 and r4['batch_id']==one['batch_id'] and r4['results']['douyin']['job_id']==one['job_id'],(code,r4)
            b4=verify(t4,r4,1);settle(t4,b4,1)
            assert db('SELECT count(*) FROM vr2_social_publish_intents WHERE task_id=CAST(:t AS uuid)',{'t':t4})==(0,)
            assert resolve(t4,one['job_id'],{**receipt,'external_id':'7412345678901234570'})[0]==200
            assert state(t4,b4)==('succeeded','published',None,1)
            code,replay=api(f'/tasks/{t4}/actions/publish',single);assert code==200 and replay['job_id']==one['job_id'] and replay['state']=='published',replay
            assert api(f'/tasks/{t4}/actions/publish',{**single,'meta':{'title':'different'}})[0]==409
            assert counters()==before and sorted((runtime/'calls').read_text().splitlines())==sorted(before_calls.splitlines()+['mixed-ok','pure-ks','mixed-ok'])
            print('PASS: pure SAU single and multi-target native batches, rollback, stable original IDs, facade history and operator settlement without legacy intents or replay',flush=True)
            # Exercise the actual automatic Publish stage, not a second call to
            # the manual batch endpoint. The parent lease must be converted
            # to an exact durable batch ticket before children can run.
            def auto_seed(platforms, via_action):
                task,payload=seed();meta=payload['platform_meta']['bilibili']
                db("UPDATE tasks SET publish_meta_json=CAST(:m AS jsonb),publish_meta_sha256=:sha,publish_meta_revision=1 WHERE id=CAST(:t AS uuid)",{'t':task,'m':json.dumps(meta),'sha':hashlib.sha256(json.dumps(meta).encode()).hexdigest()})
                if via_action:
                    db("INSERT INTO app_settings(key,value_json,version) VALUES('subtitle.auto_profile',CAST(:v AS jsonb),1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",{'v':json.dumps({'auto_publish':True,'auto_publish_platforms':platforms,'publish_typeid_mode':'meta','auto_dub_p2':False,'publish_use_youtube_cover':False})})
                    code,r=api(f'/tasks/{task}/actions/auto_publish',{});assert code==200,(code,r)
                    job=r['job_id'];flow=db('SELECT workflow_id::text FROM operation_inbox WHERE id=CAST(:j AS uuid)',{'j':job})[0]
                else:
                    flow,job=str(uuid.uuid4()),str(uuid.uuid4())
                    db("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(CAST(:w AS uuid),CAST(:t AS uuid),1,'running','publish',CAST(:c AS jsonb))",{'w':flow,'t':task,'c':json.dumps({'auto_publish':True,'auto_publish_platforms':platforms})})
                    db("INSERT INTO operation_inbox(id,workflow_id,task_id,stage,generation,operation_key,request_json,max_attempts,runtime_engine) VALUES(CAST(:j AS uuid),CAST(:w AS uuid),CAST(:t AS uuid),'publish',1,:op,'{}',1,'rust-v2')",{'j':job,'w':flow,'t':task,'op':'vr2:'+flow+':publish:1'})
                return task,flow,job
            def parent_state(flow,job):
                return db("SELECT w.state,i.status,i.external_ticket,i.result_json FROM pipeline_runs w JOIN operation_inbox i ON i.workflow_id=w.id WHERE w.id=CAST(:w AS uuid) AND i.id=CAST(:j AS uuid)",{'w':flow,'j':job})
            def auto_batch(task):
                rows=db("SELECT id::text,request_json FROM publish_batches WHERE task_id=CAST(:t AS uuid)",{'t':task},True)
                return rows[0] if rows else None
            def run_auto(task,flow,job,n):
                with (root.parent/'auto-batch-worker.log').open('a') as out:
                    core=subprocess.Popen([str(worker),'worker'],env={**os.environ,'VIDEOROLL_V2_WORKERS':'1','RUST_LOG':'warn'},stdout=out,stderr=subprocess.STDOUT)
                    try:
                        wait(lambda:auto_batch(task) is not None or parent_state(flow,job)[1]=='unknown')
                        pair=auto_batch(task);assert pair is not None,parent_state(flow,job)
                        batch,req=pair;generation=db('SELECT generation FROM pipeline_runs WHERE id=CAST(:w AS uuid)',{'w':flow})[0];assert req['auto_parent']=={'workflow_id':flow,'job_id':job,'generation':generation},req
                        wait(lambda:parent_state(flow,job)[1]=='unknown')
                        assert parent_state(flow,job)[:3]==('unknown','unknown','native-batch:'+batch),parent_state(flow,job)
                        assert db("SELECT count(*) FROM publish_jobs WHERE batch_id=CAST(:b AS uuid)",{'b':batch})==(n,)
                        return batch
                    finally:
                        core.terminate()
                        try:core.wait(timeout=8)
                        except subprocess.TimeoutExpired:core.kill();core.wait(timeout=5)
            before_auto=counters();calls_before=(runtime/'calls').read_text().splitlines()
            # Old UI auto action reads current settings and selects the newest
            # active/validated SAU account, without an account in the request.
            ta,wa,ja=auto_seed(['douyin'],True);ba=run_auto(ta,wa,ja,1)
            assert state(ta,ba)==('failed','failed','PUBLISH_FAILED',0),state(ta,ba)
            dj,account_id=db("SELECT id::text,account_id::text FROM publish_jobs WHERE batch_id=CAST(:b AS uuid) AND platform='douyin'",{'b':ba});assert account_id==good
            assert resolve(ta,dj,{**receipt,'external_id':'7412345678901234571'})[0]==200
            assert parent_state(wa,ja)[:3]==('succeeded','succeeded','native-batch:'+ba) and state(ta,ba)==('succeeded','published',None,1)
            # A pipeline-origin stage with both targets; child Bilibili stage
            # and SAU worker settle their original rows. No second parent flow.
            tb,wb,jb=auto_seed(['bilibili','douyin'],False);bb=run_auto(tb,wb,jb,2)
            assert state(tb,bb)==('partial_failed','publishing','PUBLISH_PARTIAL_FAILURE',0),state(tb,bb)
            assert db("SELECT count(*) FROM publish_jobs WHERE batch_id=CAST(:b AS uuid) AND platform='bilibili' AND state='published'",{'b':bb})==(1,)
            dj,aid=db("SELECT id::text,account_id::text FROM publish_jobs WHERE batch_id=CAST(:b AS uuid) AND platform='douyin'",{'b':bb});assert aid==good
            assert resolve(tb,dj,{**receipt,'external_id':'7412345678901234572'})[0]==200
            assert parent_state(wb,jb)[:3]==('succeeded','succeeded','native-batch:'+bb) and state(tb,bb)==('succeeded','published',None,1)
            assert counters()['submit']==before_auto['submit']+1 and (runtime/'calls').read_text().splitlines()==calls_before+['mixed-ok','mixed-ok']
            assert db("SELECT count(*) FROM operation_inbox WHERE workflow_id=CAST(:w AS uuid) AND stage='cleanup'",{'w':wb})==(0,)
            assert api(f'/tasks/{tb}/actions/auto_publish',{})[1]['status']=='complete'
            print('PASS: real automatic pure SAU and mixed Publish stages, current-profile account selection, exact durable parent tickets, original receipts and batch-only cleanup',flush=True)
            # Explicit P2 from an older successful generation must not be
            # silently dropped when the original auto action starts Publish.
            tc,payload=seed('two-part');meta=payload['platform_meta']['bilibili'];key=payload['video_key']
            db("UPDATE tasks SET publish_meta_json=CAST(:m AS jsonb),publish_meta_sha256=:sha,publish_meta_revision=1 WHERE id=CAST(:t AS uuid)",{'t':tc,'m':json.dumps(meta),'sha':hashlib.sha256(json.dumps(meta).encode()).hexdigest()})
            dub=f'v2/dub/{tc}/dub.mp4';srt=f'v2/sub/{tc}/subtitle.srt'
            (root/dub).parent.mkdir(parents=True,exist_ok=True);(root/dub).write_bytes((root/key).read_bytes())
            (root/srt).parent.mkdir(parents=True,exist_ok=True);(root/srt).write_text('1\n00:00:00,000 --> 00:00:01,000\n测试\n')
            for kind,k in [('video_raw',key),('video_dubbed',dub),('subtitle_srt',srt)]:
                db('INSERT INTO assets(id,task_id,kind,storage_key) VALUES(CAST(:a AS uuid),CAST(:t AS uuid),CAST(:kind AS asset_kind),:k)',{'a':str(uuid.uuid4()),'t':tc,'kind':kind,'k':k})
            old,oldjob=str(uuid.uuid4()),str(uuid.uuid4())
            db("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(CAST(:w AS uuid),CAST(:t AS uuid),1,'succeeded','dubbing','{}'::jsonb)",{'w':old,'t':tc})
            db("""INSERT INTO operation_inbox(id,operation_key,status,runtime_engine,workflow_id,task_id,stage,generation,dubbing_source_video_key,dubbing_source_srt_key,dubbing_output_video_key,dubbing_model_status,dubbing_model_job_id,dubbing_run_created_at)
            VALUES(CAST(:j AS uuid),:op,'succeeded','rust-v2',CAST(:w AS uuid),CAST(:t AS uuid),'dubbing',1,:raw,:srt,:dub,'succeeded',CAST(:model AS uuid),now())""",{'j':oldjob,'op':'auto-batch-dub:'+oldjob,'w':old,'t':tc,'raw':key,'srt':srt,'dub':dub,'model':str(uuid.uuid4())})
            db("UPDATE app_settings SET value_json=CAST(:v AS jsonb) WHERE key='subtitle.auto_profile'",{'v':json.dumps({'auto_publish':True,'auto_publish_platforms':['bilibili','douyin'],'publish_typeid_mode':'meta','auto_dub_p2':False,'publish_use_youtube_cover':False})})
            assert api(f'/tasks/{tc}/actions/auto_publish?dubbing_job_id={uuid.uuid4()}',{})[0]==409
            code,rc=api(f'/tasks/{tc}/actions/auto_publish?dubbing_job_id={oldjob}',{});assert code==200,(code,rc)
            jc=rc['job_id'];wc=db('SELECT workflow_id::text FROM operation_inbox WHERE id=CAST(:j AS uuid)',{'j':jc})[0]
            pre=counters();bc=run_auto(tc,wc,jc,2)
            assert counters()['submit']==pre['submit']+1 and counters()['complete']==pre['complete']+2
            dj,aid=db("SELECT id::text,account_id::text FROM publish_jobs WHERE batch_id=CAST(:b AS uuid) AND platform='douyin'",{'b':bc});assert aid==good
            assert resolve(tc,dj,{**receipt,'external_id':'7412345678901234573'})[0]==200
            assert parent_state(wc,jc)[:3]==('succeeded','succeeded','native-batch:'+bc) and state(tc,bc)==('succeeded','published',None,1)
            print('PASS: original automatic action preserves explicitly selected prior successful P2 receipt and sends exactly two Bilibili parts',flush=True)




        finally:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
        assert not list(cookies.glob('*.json'))
    print('PASS: mixed batch real mock SAU execution, both completion orders, unknown/unverified receipt quarantine and no premature cleanup',flush=True)
