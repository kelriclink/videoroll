"""Positive V1 media and task admission coverage on the disposable smoke DB."""
import hashlib, io, json, os, subprocess, uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlsplit
import psycopg
from PIL import Image


def probe(base, token):
    url = os.environ['DATABASE_URL']
    assert base == 'http://127.0.0.1:18080'
    assert urlsplit(url).hostname == '127.0.0.1' and urlsplit(url).path == '/videoroll_test'
    assert token == 'v2-local-test-only-0123456789abcdef'
    root = Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT'])
    assert root == Path(__file__).resolve().parents[1]/'target/v2-smoke-objects'
    records = []
    def db(sql, params=(), fetch=False):
        with psycopg.connect(url) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchone() if fetch else None
    def call(method, path, body=None, auth=True, headers=None):
        h = dict(headers or {})
        if auth: h['x-videoroll-v2-internal-token'] = token
        if isinstance(body, dict):
            body = json.dumps(body).encode(); h['Content-Type'] = 'application/json'
        try: response = urlopen(Request(base+'/api/v1'+path, method=method, data=body, headers=h), timeout=20)
        except HTTPError as error: response = error
        with response:
            raw = response.read(2*1024*1024)
            mime = response.headers.get('Content-Type','').split(';')[0]
            return response.status, mime, json.loads(raw) if mime == 'application/json' else raw
    def check(method, template, path, body=None, headers=None, expect=200, verify=lambda _:True):
        denied = call(method,path,body,False,headers)[0]
        assert denied in (401,403), (template,denied)
        code,mime,obj = call(method,path,body,True,headers)
        assert code == expect and verify(obj), (method,template,code,str(obj)[:400])
        records.append(dict(method=method,path=template,http_status=code,response_kind=mime,probe='positive',issue=None,fixture_asserted=True,unauthorized_status=denied,json_fields=sorted(obj) if isinstance(obj,dict) else []))
        return obj
    def fresh(status='created'):
        t=str(uuid.uuid4())
        db("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own',%s::task_status,'rust')",(t,status))
        return t
    def asset(t,kind,ext,raw):
        a=str(uuid.uuid4());key=f'v2/contract/{t}/{a}.{ext}';sha=hashlib.sha256(raw).hexdigest()
        f=root/key;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(raw)
        with psycopg.connect(url) as c:
            c.execute('INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s::asset_kind,%s,%s,%s)',(a,t,kind,key,sha,len(raw)))
            c.execute("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES(%s,%s,%s,'live',now()+interval '2 days')",(key,sha,len(raw)))
            c.execute("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(%s,'task',%s,%s)",(key,t,kind))
        return a,key
    def multipart(filename,mime,data):
        boundary='contract-media-'+uuid.uuid4().hex
        raw=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
        return raw,{'Content-Type':'multipart/form-data; boundary='+boundary}
    media=root.parent/'contract-v3-media.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-f','lavfi','-i','color=c=blue:s=128x96:d=1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(media)],check=True,timeout=30)
    video=media.read_bytes();digest=hashlib.sha256(video).hexdigest()
    image=io.BytesIO();Image.new('RGB',(48,32),(20,40,60)).save(image,format='PNG')
    t=fresh();prefix='/tasks/'+t
    data,h=multipart('fixture.mp4','video/mp4',video)
    raw=check('POST','/tasks/{task_id}/upload/video',prefix+'/upload/video',data,h,expect=201,verify=lambda x:x['task_id']==t and x['kind']=='video_raw' and x['sha256']==digest)
    assert db('SELECT status::text FROM tasks WHERE id=%s',(t,),True)==('ingested',)
    assert (root/raw['storage_key']).read_bytes()==video
    data,h=multipart('fixture.png','image/png',image.getvalue())
    cover=check('POST','/tasks/{task_id}/upload/cover',prefix+'/upload/cover',data,h,verify=lambda x:x['task_id']==t and x['kind']=='cover_image')
    with Image.open(root/cover['storage_key']) as decoded:assert decoded.size==(48,32)
    t2=fresh('rendered');a,key=asset(t2,'video_final','mp4',video);p='/tasks/'+t2+'/assets/'+a
    check('GET','/tasks/{task_id}/assets/{asset_id}/download',p+'/download',verify=lambda x:x==video)
    check('GET','/tasks/{task_id}/assets/{asset_id}/stream',p+'/stream',headers={'Range':'bytes=0-15'},expect=206,verify=lambda x:x==video[:16])
    assert call('GET',prefix+'/assets/'+a+'/download')[0]==404
    playout=check('POST','/tasks/{task_id}/assets/{asset_id}/playout',p+'/playout',{},verify=lambda x:x['task_id']==t2 and x['asset_id']==a and x['status']=='ready' and x['source_checksum']==digest)
    assert call('POST',p+'/playout',{})[2]['id']==playout['id']
    assert db('SELECT count(*) FROM playout_asset_links WHERE id=%s AND asset_id=%s',(playout['id'],a),True)==(1,)
    check('DELETE','/tasks/{task_id}/assets/{asset_id}',p,verify=lambda x:x['deleted'] is True)
    assert db('SELECT count(*) FROM assets WHERE id=%s',(a,),True)==(0,)
    assert db('SELECT count(*) FROM playout_asset_links WHERE id=%s',(playout['id'],),True)==(0,)
    assert db('SELECT count(*) FROM storage_object_refs WHERE storage_key=%s',(key,),True)==(0,)
    assert (root/key).is_file(), 'deferred GC must not unlink synchronously'
    # Separate task per stage: no worker consumption or external calls.
    def stage_task():
        t=fresh('ingested');_,key=asset(t,'video_raw','mp4',video);return t,key
    def job_ok(t,name,obj):
        return db('SELECT task_id::text,stage,status FROM operation_inbox WHERE id=%s',(obj['job_id'],),True)==(t,name,'pending')
    for action in ('subtitle','subtitle_resume','auto_subtitle_handoff'):
        t,_=stage_task();body={'burn_in':True,'translate_enabled':False} if action=='subtitle' else {}
        check('POST','/tasks/{task_id}/actions/'+action,'/tasks/'+t+'/actions/'+action,body,verify=lambda x:job_ok(t,'subtitle',x))
    # Dubbing uses independent prerequisite assets and a persisted native job.
    def subtitle_ready():
        t,key=stage_task()
        asset(t,'subtitle_srt','srt',b'1\n00:00:00,000 --> 00:00:00,800\nfixture\n\n')
        return t,key
    def prior_stage(t,name,status):
        w,j=[str(uuid.uuid4()) for _ in range(2)]
        with psycopg.connect(url) as conn:
            conn.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(%s,%s,1,%s,%s,'{}'::jsonb)",(w,t,status,name))
            conn.execute("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state) VALUES(%s,%s,%s,%s,1,%s,%s)",(j,w,t,name,'contract-v3:'+j,status))
        return w,j
    t,key=subtitle_ready();w,j=prior_stage(t,'subtitle','succeeded')
    body={'subtitle_job_id':j,'subtitle_video_key':key,'operation_key':'contract-v3:'+t,'model_path':'youdub:voxcpm2','voice':'reference','preview_only':False}
    path='/tasks/'+t+'/actions/dubbing'
    dub=check('POST','/tasks/{task_id}/actions/dubbing',path,body,verify=lambda x:job_ok(t,'dubbing',x))
    assert call('POST',path,body)[2]['job_id']==dub['job_id']
    assert db("SELECT count(*) FROM operation_inbox WHERE task_id=%s AND stage='dubbing'",(t,),True)==(1,)
    check('POST','/tasks/{task_id}/dubbing_jobs/{job_id}/control','/tasks/'+t+'/dubbing_jobs/'+dub['job_id']+'/control?action=pause',{},verify=lambda x:x['job_id']==dub['job_id'] and x['status']=='paused')
    assert db('SELECT w.state FROM pipeline_runs w JOIN operation_inbox j ON j.workflow_id=w.id WHERE j.id=%s',(dub['job_id'],),True)==('paused',)
    t,_=subtitle_ready();w,j=prior_stage(t,'dubbing','failed')
    check('POST','/tasks/{task_id}/dubbing_jobs/{job_id}/retry','/tasks/'+t+'/dubbing_jobs/'+j+'/retry',{},verify=lambda x:x['job_id']!=j and job_ok(t,'dubbing',x))
    assert db("SELECT generation,status FROM operation_inbox WHERE task_id=%s AND stage='dubbing' ORDER BY generation",(t,),True)==(1,'failed')
    print(f'PASS: {len(records)} isolated media and stage route assertions, actual media bytes and durable stage rows',flush=True)
    return records
