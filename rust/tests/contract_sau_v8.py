"""Native SAU intake with an isolated executable; no browser or publisher worker."""
import json,os,subprocess,tempfile,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
import psycopg


def probe(root,env,port,executable,call):
    url=env['DATABASE_URL'];u=urlsplit(url)
    assert u.hostname=='127.0.0.1' and u.path=='/videoroll_test'
    assert env['VIDEOROLL_V2_INTERNAL_TOKEN']=='v2-local-test-only-0123456789abcdef'
    storage=Path(env['VIDEOROLL_V2_STORAGE_ROOT']);assert storage==root/'target/v2-smoke-objects'
    assert env['VIDEOROLL_V2_SAU_BIND']==f'127.0.0.1:{port}'
    records=[]
    def db(query,params=(),fetch=False):
        with psycopg.connect(url) as c:
            cur=c.execute(query,params);return cur.fetchone() if fetch else None
    def check(template,path,body=None,form=False,verify=lambda _:True):
        denied=call('POST',path,body,auth=False,form=form)[0];assert denied in (401,403),(template,denied)
        code,mime,obj=call('POST',path,body,form=form)
        assert code==200 and verify(obj),(template,code,str(obj)[:300])
        assert 'isolated-v8-cookie' not in str(obj) and 'dbplain:v1:' not in str(obj)
        records.append(dict(service='social_publisher',method='POST',path=template,http_status=code,response_kind=mime,probe='positive',issue=None,fixture_asserted=True,unauthorized_status=denied,json_fields=sorted(obj)))
        return obj
    def wait_for(predicate):
        for _ in range(160):
            if predicate():return
            time.sleep(.1)
        raise AssertionError('SAU fixture did not settle')
    with tempfile.TemporaryDirectory(prefix='contract-sau-v8-') as tmp:
        runtime=Path(tmp);cookies=runtime/'cookies';cookies.mkdir(mode=0o700)
        mock=runtime/'sau';mock.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
p,mode,flag,n,*rest=sys.argv[1:]
assert flag=='--account' and mode in ('check','login')
key=Path('cookies')/f'{p}_{n}.json'
if mode=='check':
    assert key.stat().st_mode&0o777==0o600
    assert json.loads(key.read_text())['cookies']
else:
    assert rest==['--headed'] and not key.exists()
    key.write_text(json.dumps({'cookies':[{'name':'session','value':'isolated-v8-cookie'}],'origins':[]}))
with open('calls','a') as f:f.write(f'{p}:{mode}:{n}\\n')
''');mock.chmod(0o700)
        local={**env,'VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK':'true','VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN':'true','VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH':'true','VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX':'false','SAU_RUNTIME_DIR':str(runtime),'SAU_COOKIES_DIR':str(cookies),'SAU_EXECUTABLE':str(mock),'STORAGE_ROOT':str(storage),'SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS':'5','SOCIAL_LOGIN_TIMEOUT_SECONDS':'5','SOCIAL_LOGIN_DISPLAY':':991','SOCIAL_LOGIN_BROWSER_URL':'/social-login/vnc.html?autoconnect=1&resize=scale&path=/social-login/websockify'}
        proc=subprocess.Popen([str(executable)],env=local,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            def ready():
                assert proc.poll() is None,'SAU fixture exited'
                try:return call('GET','/health',auth=False)[0]==200
                except OSError:return False
            wait_for(ready)
            name='contract-v8-'+uuid.uuid4().hex[:12]
            raw=json.dumps({'cookies':[{'name':'session','value':'isolated-v8-cookie'}],'origins':[]}).encode()
            data=(f'--vr2contract\r\nContent-Disposition: form-data; name="account_name"\r\n\r\n{name}\r\n--vr2contract\r\nContent-Disposition: form-data; name="file"; filename="state.json"\r\nContent-Type: application/json\r\n\r\n').encode()+raw+b'\r\n--vr2contract--\r\n'
            r=check('/accounts/{platform}','/accounts/douyin',data,True,lambda x:x['account']['name']==name and x['account']['check_state']=='queued')
            aid=r['account']['id'];op=r['check_job_id']
            wait_for(lambda:db('SELECT status FROM operation_inbox WHERE id=%s',(op,),True)==('completed',))
            assert db('SELECT check_state FROM accounts WHERE id=%s',(aid,),True)==('valid',)
            r=check('/accounts/{account_id}/check','/accounts/'+aid+'/check',{},verify=lambda x:x['account']['id']==aid and x['check_job_id']!=op)
            wait_for(lambda:db('SELECT status FROM operation_inbox WHERE id=%s',(r['check_job_id'],),True)==('completed',))
            assert db('SELECT check_state FROM accounts WHERE id=%s',(aid,),True)==('valid',)
            login_name=name+'-login'
            r=check('/login-sessions/{platform}','/login-sessions/kuaishou',{'account_name':login_name},verify=lambda x:x['state']=='starting' and x['account_name']==login_name)
            wait_for(lambda:db('SELECT status,result_json->>\'state\' FROM operation_inbox WHERE id=%s',(r['id'],),True)==('completed','succeeded'))
            assert db('SELECT check_state,is_active FROM accounts WHERE name=%s AND platform::text=\'kuaishou\'',(login_name,),True)==('valid',True)
            t=str(uuid.uuid4());key=f'v2/contract-sau/{t}/video.mp4';p=storage/key;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((root/'target/contract-v3-media.mp4').read_bytes())
            with psycopg.connect(url) as c:
                c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','approved','rust')",(t,))
                c.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final',%s)",(uuid.uuid4(),t,key))
            body={'platform':'douyin','task_id':t,'account_id':aid,'video':{'type':'storage','key':key},'meta':{'title':'contract v8','desc':'fixture','tags':['fixture']},'platform_options':{}}
            r=check('/sau/{platform}/publish','/sau/douyin/publish',body,verify=lambda x:x['state']=='submitting')
            assert call('POST','/sau/douyin/publish',body)[2]['job_id']==r['job_id']
            assert db('SELECT state::text,task_id::text FROM publish_jobs WHERE id=%s',(r['job_id'],),True)==('submitting',t)
            assert db('SELECT status FROM outbox_events WHERE operation_key=%s',('publish-job:'+r['job_id'],),True)==('native_pending',)
            assert db('SELECT count(*) FROM publish_jobs WHERE task_id=%s',(t,),True)==(1,)
            assert sorted((runtime/'calls').read_text().splitlines())==sorted([f'douyin:check:{name}',f'douyin:check:{name}',f'kuaishou:login:{login_name}'])
            assert not list(cookies.glob('*.json'))
        finally:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
    print('PASS: four standalone SAU native intake routes, mocked account/login CLI, native publication outbox without dispatch',flush=True)
    return records
