"""Positive original RenderWorker routes on disposable PostgreSQL + real HTTP.
The coordinator is real; the synthetic worker and MP4 are local fixtures.
"""
import hashlib, json, os, uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import psycopg


def probe(base, token):
    url=os.environ['DATABASE_URL']; parsed=urlsplit(url)
    assert parsed.hostname=='127.0.0.1' and parsed.path=='/videoroll_test'
    assert base=='http://127.0.0.1:18080' and token=='v2-local-test-only-0123456789abcdef'
    root=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT'])
    assert root==Path(__file__).resolve().parents[1]/'target/v2-smoke-objects'
    media=(root.parent/'contract-v3-media.mp4').read_bytes()
    sha=hashlib.sha256(media).hexdigest()
    records=[]; prefix='/api/render-workers/v1'; template='/render-workers/v1'
    def sql(query,params=(),fetch=False):
        with psycopg.connect(url) as c:
            row=c.execute(query,params)
            return row.fetchone() if fetch else None
    def call(method,path,body=None,auth=None,headers=None):
        h=dict(headers or {})
        if auth=='admin':h['x-videoroll-v2-internal-token']=token
        elif auth:h['Authorization']='Bearer '+auth
        if isinstance(body,dict):body=json.dumps(body).encode();h['Content-Type']='application/json'
        try:r=urlopen(Request(base+path,data=body,method=method,headers=h),timeout=20)
        except HTTPError as exc:r=exc
        with r:
            mime=r.headers.get('Content-Type','').split(';')[0];raw=r.read(2*1024*1024)
            return r.status,mime,json.loads(raw) if mime=='application/json' else raw
    def check(method,route,path,body=None,auth=None,headers=None,expect=200,verify=lambda x:True,anonymous=True):
        denied=call(method,path,body,headers=headers)[0] if anonymous else None
        assert not anonymous or denied in (401,403),(route,denied)
        code,mime,obj=call(method,path,body,auth,headers)
        assert code==expect and verify(obj),(method,route,code,str(obj)[:300])
        record=dict(method=method,path=template+route,http_status=code,response_kind=mime,probe='positive',issue=None,fixture_asserted=True,json_fields=sorted(obj) if isinstance(obj,dict) else [])
        if anonymous:record['unauthorized_status']=denied
        records.append(record);return obj
    invite=call('POST','/api/v2/render/enrollments',{'label':'contract-v4','ttl_minutes':10},'admin')
    assert invite[0]==201
    body={'worker_key':'contract-v4-'+uuid.uuid4().hex,'name':'isolated render worker','platform':'linux','max_concurrency':1,'capabilities':{'encoders':['h264']},'enrollment_token':invite[2]['token']}
    worker=check('POST','/enroll',prefix+'/enroll',body,anonymous=False,verify=lambda x:len(x['credential'])==64)
    wid=worker['worker']['id'];cred=worker['credential']
    assert call('POST',prefix+'/enroll',body)[0]==401
    assert sql('SELECT status FROM render_worker_enrollments WHERE id=%s',(invite[2]['id'],),True)==('consumed',)
    local={k:v for k,v in body.items() if k!='enrollment_token'};local['worker_key']='contract-local-'+uuid.uuid4().hex
    old=check('POST','/local-enroll',prefix+'/local-enroll',local,os.environ['ADMIN_BOOTSTRAP_SECRET'],verify=lambda x:x['credential'].startswith('vrw_') and x['worker']['labels']['local'])
    assert sql('SELECT runtime_engine FROM render_workers WHERE id=%s',(old['worker']['id'],),True)==('python',)
    check('POST','/{worker_id}/heartbeat',prefix+'/'+wid+'/heartbeat',{'status':'online','resources':{'fixture':True},'capabilities':{'encoders':['h264']},'active_execution_ids':[]},cred,verify=lambda x:x['id']==wid and x['resources']['fixture'])
    assert sql("SELECT resources->>'fixture' FROM render_workers WHERE id=%s",(wid,),True)==('true',)
    def seed():
        t,w,j,e=[str(uuid.uuid4()) for _ in range(4)]
        with psycopg.connect(url) as c:
            c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority) VALUES(%s,'local','own','subtitle_ready','rust',2147483647)",(t,))
            for kind,extension,raw in [('video_raw','mp4',media),('subtitle_ass','ass',b'[Script Info]\nScriptType: v4.00+\n')]:
                key=f'v2/contract-worker/{t}/{kind}.{extension}';p=root/key;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
                c.execute('INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s::asset_kind,%s,%s,%s)',(uuid.uuid4(),t,kind,key,hashlib.sha256(raw).hexdigest(),len(raw)))
            c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(%s,%s,1,'running','render',%s)",(w,t,json.dumps({'render':{'burn_in':True,'soft_sub':False},'auto_publish':False,'auto_dub_p2':False})))
            c.execute("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,external_ticket) VALUES(%s,%s,%s,'render',1,%s,'awaiting',%s)",(j,w,t,'contract-worker:'+j,e))
            c.execute("INSERT INTO render_executions(id,task_id,workflow_id,job_id,runtime_engine,state,attempt) VALUES(%s,%s,%s,%s,'rust-v2','queued',0)",(e,t,w,j))
        return t,w,j,e
    t,w,j,e=seed();claim_path=prefix+'/'+wid+'/claim';claim_body={'available_slots':1,'accepted_transfer_modes':['http'],'available_encoders':['h264']}
    claimed=check('POST','/{worker_id}/claim',claim_path,claim_body,cred,verify=lambda x:x['execution']['id']==e and x['render_spec']['task_id']==t and x['render_spec']['mode']=='burn_in')
    assert call('POST',claim_path,claim_body,cred)[2]['execution'] is None
    fence=claimed['execution']['fence_token'];ep=prefix+'/executions/'+e;et='/executions/{execution_id}'
    assert len(fence)==64
    check('GET',et+'/artifacts/{role}',ep+'/artifacts/input',auth=cred,headers={'Range':'bytes=0-15'},expect=206,verify=lambda x:x==media[:16])
    check('GET',et+'/cancel',ep+'/cancel',auth=cred,verify=lambda x:x['cancel_requested'] is False)
    check('POST',et+'/heartbeat',ep+'/heartbeat',{'fence_token':fence,'progress':12},cred,verify=lambda x:x['progress']==12)
    assert call('POST',ep+'/heartbeat',{'fence_token':'invalid','progress':99},cred)[0]==409
    check('POST',et+'/progress',ep+'/progress',{'fence_token':fence,'progress':42,'metrics':{'fps':18}},cred,verify=lambda x:x['progress']==42)
    assert sql('SELECT log_tail FROM render_executions WHERE id=%s',(e,),True)==(None,)
    check('POST',et+'/logs',ep+'/logs',{'fence_token':fence,'text':'contract-render-log'},cred,verify=lambda x:x['id']==e)
    assert call('POST',ep+'/logs',{'fence_token':fence,'text':'second-line'},cred)[0]==200
    row=sql('SELECT progress,metrics,log_tail FROM render_executions WHERE id=%s',(e,),True)
    assert row[:2]==(42,{'fps':18}) and 'contract-render-log' in row[2] and row[2].endswith('second-line')
    assert call('GET',ep+'/output?fence_token='+fence,auth=cred)[0]==404
    boundary='contract-render-'+uuid.uuid4().hex
    upload=(f'--{boundary}\r\nContent-Disposition: form-data; name="fence_token"\r\n\r\n{fence}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode()+media+f'\r\n--{boundary}--\r\n'.encode()
    out=check('POST',et+'/output',ep+'/output',upload,cred,{'Content-Type':'multipart/form-data; boundary='+boundary},verify=lambda x:x['task_id']==t and x['kind']=='video_final' and x['sha256']==sha)
    aid=out['id'];assert (root/out['storage_key']).read_bytes()==media
    check('GET',et+'/output',ep+'/output?fence_token='+fence,auth=cred,verify=lambda x:x['id']==aid)
    done={'fence_token':fence,'output_asset_id':aid,'output':{}}
    check('POST',et+'/complete',ep+'/complete',done,cred,verify=lambda x:x['id']==e and x['state']=='completed')
    assert call('POST',ep+'/complete',done,cred)[0]==200
    assert sql('SELECT state,lease_until,uploaded_asset_id::text FROM render_executions WHERE id=%s',(e,),True)==('completed',None,aid)
    assert sql('SELECT status FROM operation_inbox WHERE id=%s',(j,),True)==('succeeded',)
    t,w,j,e=seed();claim=call('POST',claim_path,claim_body,cred);assert claim[0]==200 and claim[2]['execution']['id']==e
    fence=claim[2]['execution']['fence_token'];ep=prefix+'/executions/'+e
    check('POST',et+'/fail',ep+'/fail',{'fence_token':fence,'error':'fixture terminal failure','retryable':False},cred,verify=lambda x:x['state']=='failed')
    assert sql('SELECT status FROM operation_inbox WHERE id=%s',(j,),True)==('failed',)
    assert len({(r['method'],r['path']) for r in records})==len(records)
    print(f'PASS: {len(records)} isolated original RenderWorker route assertions, real bytes, lease, receipt and stage state',flush=True)
    return records
