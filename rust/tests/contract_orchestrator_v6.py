"""Original browser gateway, desktop and remote admission routes, disposable only."""
import json,os,uuid,hashlib,threading
from pathlib import Path
from urllib.parse import urlsplit
import psycopg
from contract_services_v5 import Fixtures, Backend
from http.server import ThreadingHTTPServer


def probe(root,send):
    url=os.environ['DATABASE_URL'];u=urlsplit(url)
    assert u.hostname=='127.0.0.1' and u.path=='/videoroll_test'
    assert os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']=='v2-local-test-only-0123456789abcdef'
    assert os.environ['VIDEOROLL_V2_SOCIAL_PUBLISHER_URL']=='http://127.0.0.1:18085'
    assert os.environ['VIDEOROLL_V2_TEST_BILIBILI_BASE']=='http://127.0.0.1:18096/'
    storage=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT']);assert storage==root/'target/v2-smoke-objects'
    records=[]
    def db(query,params=(),fetch=False):
        with psycopg.connect(url) as c:
            cur=c.execute(query,params)
            return cur.fetchone() if fetch else None
    def call(method,path,body=None,**kwargs):return send(method,'/api/v1'+path,body,**kwargs)
    def check(method,template,path,body=None,verify=lambda _:True,expect=200,kwargs=None,denial_kwargs=None):
        kwargs=kwargs or {};denial_kwargs=denial_kwargs or {}
        denied=call(method,path,body,authenticated=False,**denial_kwargs)[0]
        assert denied in (401,403),(method,template,denied)
        code,mime,obj=call(method,path,body,**kwargs)
        assert code==expect and verify(obj),(method,template,path,code,str(obj)[:300])
        records.append(dict(method=method,path=template,http_status=code,response_kind=mime,json_fields=sorted(obj) if isinstance(obj,dict) else [],probe='positive',issue=None,fixture_asserted=True,unauthorized_status=denied))
        return obj
    # Wildcard boundaries: one allowed concrete route per verb, and the
    # same method on a disallowed path must still fail closed.
    def gateway(method,prefix,path,body=None,verify=lambda _:True):
        bad=call(method,'/'+prefix+'/not-an-allowed-operation',body)[0];assert bad==404,(prefix,method,bad)
        obj=check(method,'/'+prefix+'/{service_path:path}',path,body,verify)
        records[-1]['concrete_path']=path;records[-1]['non_allowlisted_status']=bad
        return obj
    old=db("SELECT value_json,version FROM app_settings WHERE key='bilibili.publish'",fetch=True)
    try:
        gateway('PUT','bilibili','/bilibili/publish/settings',{'upload_workers':3},lambda x:x['upload_workers']==3)
        gateway('GET','bilibili','/bilibili/publish/settings',verify=lambda x:x['upload_workers']==3)
        assert db("SELECT value_json->>'upload_workers' FROM app_settings WHERE key='bilibili.publish'",fetch=True)==('3',)
    finally:
        if old:db("UPDATE app_settings SET value_json=%s,version=%s WHERE key='bilibili.publish'",(json.dumps(old[0]),old[1]))
        else:db("DELETE FROM app_settings WHERE key='bilibili.publish'")
    f=Fixtures(root)
    member=ThreadingHTTPServer(('127.0.0.1',18096),Backend)
    threading.Thread(target=member.serve_forever,daemon=True).start()
    try:
        f.configure('bilibili_publisher',{})
        gateway('POST','bilibili','/bilibili/archive/type/recommend',{'text':'contract category'},lambda x:x['ok'] and x['typeid']==180 and x['used_text']=='contract category')
    finally:
        member.shutdown();member.server_close();f.close()
    # Use a persisted dictionary source and term rather than a network provider.
    slug='contract-gateway-'+uuid.uuid4().hex[:12]
    s,e=str(uuid.uuid4()),str(uuid.uuid4())
    with psycopg.connect(url) as c:
        c.execute("INSERT INTO translation_dictionary_sources(id,slug,name,source_lang,target_lang) VALUES(%s,%s,'Gateway fixture','en','zh')",(s,slug))
        c.execute("INSERT INTO translation_dictionary_entries(id,source_id,source_lang,target_lang,term,normalized_term,translation_text) VALUES(%s,%s,'en','zh',%s,%s,'测试网关')",(e,s,slug,slug.replace('-',' ')))
    gateway('GET','subtitle','/subtitle/dictionaries/sources?q='+slug+'&limit=1',verify=lambda x:any(v['id']==s for v in x))
    gateway('POST','subtitle','/subtitle/dictionaries/lookup',{'term':slug,'source_lang':'en','target_lang':'zh','exact':True},lambda x:any(v['id']==e for v in x['results']))
    gateway('PUT','subtitle','/subtitle/dictionaries/sources/'+s,{'description':'updated by gateway'},lambda x:x['id']==s and x['description']=='updated by gateway')
    assert db('SELECT description FROM translation_dictionary_sources WHERE id=%s',(s,),True)==('updated by gateway',)
    gateway('DELETE','subtitle','/subtitle/dictionaries/entries/'+e,verify=lambda x:x['deleted'] is True)
    assert db('SELECT count(*) FROM translation_dictionary_entries WHERE id=%s',(e,),True)==(0,)
    src='UC'+uuid.uuid4().hex[:22]
    source=gateway('POST','youtube','/youtube/sources',{'source_type':'channel','source_id':src,'license':'authorized','auto_process':False},lambda x:x['source_id']==src)
    sid=source['id'];path='/youtube/sources/'+sid
    gateway('GET','youtube','/youtube/sources',verify=lambda x:any(v['id']==sid for v in x))
    gateway('PATCH','youtube',path,{'display_name':'Gateway source','scan_limit':2,'auto_process':False},lambda x:x['id']==sid and x['display_name']=='Gateway source' and x['scan_limit']==2)
    assert db('SELECT display_name,scan_limit FROM youtube_sources WHERE id=%s',(sid,),True)==('Gateway source',2)
    gateway('DELETE','youtube',path,verify=lambda x:x['ok'] is True)
    assert db('SELECT count(*) FROM youtube_sources WHERE id=%s',(sid,),True)==(0,)
    # Cookie-only desktop grant. Existing authenticated browser jar belongs to
    # the isolated login above; the internal token must not be sufficient.
    resource=str(uuid.uuid4())
    grant=check('POST','/desktop/grants','/desktop/grants',{'desktop_type':'login','resource_id':resource},lambda x:x['resource_id']==resource and x['reconnect_limit']==8,kwargs={'use_cookie':True})
    digest=hashlib.sha256(grant['token'].encode()).hexdigest()
    assert db('SELECT status FROM desktop_access_grants WHERE token_hash=%s',(digest,),True)==('active',)
    headers={'x-original-uri':'/social-login/vnc.html?grant='+grant['token']+'&resource='+resource}
    check('GET','/desktop/authorize','/desktop/authorize',verify=lambda _:True,expect=204,kwargs={'use_cookie':True,'headers':headers},denial_kwargs={'headers':headers})
    assert call('GET','/desktop/authorize',use_cookie=True,headers={'x-original-uri':'/social-publish/vnc.html?grant='+grant['token']+'&resource='+resource})[0]==403
    # Remote POST is independently authenticated and idempotent. The retired
    # GET remains a deliberate 410 in the ordinary probe.
    old=db("SELECT value_json,version FROM app_settings WHERE key='remote.api'",fetch=True)
    remote='contract-v6-'+uuid.uuid4().hex;idem='contract-v6-'+uuid.uuid4().hex
    body={'url':'https://youtu.be/V6REMOTE001','proof_url':'https://example.invalid/permission'}
    assert call('PUT','/settings/api',{'token':remote})[0]==200
    try:
        h={'Authorization':'Bearer '+remote,'Idempotency-Key':idem}
        result=check('POST','/remote/auto/youtube','/remote/auto/youtube',body,lambda x:x['source_id']=='V6REMOTE001' and x['task_id'] and x['pipeline_job_id'],kwargs={'authenticated':False,'headers':h})
        assert call('POST','/remote/auto/youtube',body,authenticated=False,headers=h)[2]==result
        assert db('SELECT status,response_json FROM remote_api_requests WHERE idempotency_key=%s',(idem,),True)==('completed',result)
    finally:
        if old:db("UPDATE app_settings SET value_json=%s,version=%s WHERE key='remote.api'",(json.dumps(old[0]),old[1]))
        else:db("DELETE FROM app_settings WHERE key='remote.api'")
    print(f'PASS: {len(records)} original orchestrator gateway and credential-scoped route assertions',flush=True)
    return records
