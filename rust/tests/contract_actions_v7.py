"""V1 task actions and account proxy assertions, isolated DB and mock upstream."""
import hashlib,json,os,uuid
from pathlib import Path
from urllib.parse import urlsplit
import psycopg


def probe(root,send):
    url=os.environ['DATABASE_URL'];u=urlsplit(url)
    assert u.hostname=='127.0.0.1' and u.path=='/videoroll_test'
    assert os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']=='v2-local-test-only-0123456789abcdef'
    assert os.environ['VIDEOROLL_V2_SOCIAL_PUBLISHER_URL']=='http://127.0.0.1:18085'
    storage=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT']);assert storage==root/'target/v2-smoke-objects'
    video=(root/'target/contract-v3-media.mp4').read_bytes()
    records=[]
    def db(query,params=(),fetch=False):
        with psycopg.connect(url) as c:
            cur=c.execute(query,params);return cur.fetchone() if fetch else None
    def call(method,path,body=None,**kwargs):return send(method,'/api/v1'+path,body,**kwargs)
    def check(template,path,body=None,verify=lambda _:True,**kwargs):
        denied=call('POST',path,body,authenticated=False,**kwargs)[0]
        assert denied in (401,403),(template,denied)
        code,mime,obj=call('POST',path,body,**kwargs)
        assert code==200 and verify(obj),(template,code,str(obj)[:400])
        records.append(dict(method='POST',path=template,http_status=code,response_kind=mime,json_fields=sorted(obj) if isinstance(obj,dict) else [],probe='positive',issue=None,fixture_asserted=True,unauthorized_status=denied))
        return obj
    def setting(key):return db('SELECT value_json,version FROM app_settings WHERE key=%s',(key,),True)
    def restore(key,old):
        if old:db('UPDATE app_settings SET value_json=%s,version=%s WHERE key=%s',(json.dumps(old[0]),old[1],key))
        else:db('DELETE FROM app_settings WHERE key=%s',(key,))
    def task(status,source='local',source_url=None):
        t=str(uuid.uuid4());db("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,source_url) VALUES(%s,%s,%s,%s::task_status,'rust',%s)",(t,source,'own' if source=='local' else 'authorized',status,source_url));return t
    def asset(t,kind,key,raw):
        a=str(uuid.uuid4());p=storage/key;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
        sha=hashlib.sha256(raw).hexdigest()
        with psycopg.connect(url) as c:
            c.execute('INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,%s::asset_kind,%s,%s,%s)',(a,t,kind,key,sha,len(raw)))
            c.execute("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES(%s,%s,%s,'live',now()+interval '2 days')",(key,sha,len(raw)))
            c.execute("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(%s,'task',%s,%s)",(key,t,kind))
        return a
    # Download facade must return the exact existing media and metadata DTO,
    # not fabricate a successful download receipt or enqueue another job.
    t=task('downloaded','youtube','https://youtu.be/V7CACHE0001')
    rawkey=f'v2/raw/{t}/source.mp4';meta_key=f'v2/youtube-meta/{t}/metadata.json'
    a=asset(t,'video_raw',rawkey,video);meta={'title':'V7 cached metadata','description':'fixture','webpage_url':'https://youtu.be/V7CACHE0001','duration':1}
    m=asset(t,'metadata_json',meta_key,json.dumps(meta).encode());path=f'/tasks/{t}/actions/youtube_download'
    result=check('/tasks/{task_id}/actions/youtube_download',path,{},lambda x:x['video_asset']['id']==a and x['video_asset']['sha256']==hashlib.sha256(video).hexdigest() and x['metadata_asset']['id']==m and x['metadata']['title']==meta['title'] and x['cover_asset'] is None)
    assert call('POST',path,{})[2]==result
    assert db('SELECT count(*) FROM operation_inbox WHERE task_id=%s',(t,),True)==(0,)
    # Native selective translation pins a verified aligned source snapshot.
    t=task('subtitle_ready');w,j=str(uuid.uuid4()),str(uuid.uuid4())
    key=f'v2/sub/{t}/{j}/segments.json';srtkey=key.replace('segments.json','subtitle.srt')
    cues=[{'start_ms':100,'end_ms':400,'text':'one'},{'start_ms':500,'end_ms':900,'text':'two'}]
    snapshot=json.dumps({'source':cues,'translated':[{**r,'text':'旧译:'+r['text']} for r in cues],'translation':True},ensure_ascii=False).encode()
    asset(t,'video_raw',f'v2/raw/{t}/source.mp4',video);asset(t,'segments_json',key,snapshot)
    asset(t,'subtitle_srt',srtkey,b'1\n00:00:00,100 --> 00:00:00,400\nold one\n\n2\n00:00:00,500 --> 00:00:00,900\nold two\n')
    cfg={'subtitle':{'translate_enabled':True,'translate_provider':'openai','translate_model_provider_id':'legacy','translate_model_id':'v7-mock-translator'},'auto_publish':False,'auto_publish_platforms':[]}
    with psycopg.connect(url) as c:
        c.execute("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES(%s,%s,1,'succeeded','cleanup',%s)",(w,t,json.dumps(cfg)))
        c.execute("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,result) VALUES(%s,%s,%s,'subtitle',1,%s,'succeeded','{}')",(j,w,t,'contract-v7:'+j))
    old=setting('subtitle.translate');p=f'/tasks/{t}/actions/subtitle_retranslate'
    try:
        db("INSERT INTO app_settings(key,value_json,version) VALUES('subtitle.translate',%s,1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({'default_provider':'openai','openai':{'base_url':'http://127.0.0.1:18082/v1','model':'v7-mock-translator','api_key_enc':'dbplain:v1:isolated-v7'}}),))
        out=check('/tasks/{task_id}/actions/subtitle_retranslate',p,{'indices':[2,1,2]},lambda x:x['status']=='queued')
        assert call('POST',p,{'indices':[1,2]})[2]==out
        assert call('POST',p,{'indices':[1]})[0]==409
        row=db("SELECT i.status,w.configuration->'selective_retranslate' FROM operation_inbox i JOIN vr2_workflows w ON w.id=i.workflow_id WHERE i.id=%s",(out['job_id'],),True)
        assert row==('pending',{'indices':[1,2],'base_segments_key':key,'base_sha256':hashlib.sha256(snapshot).hexdigest(),'source_job_id':j}),row
        assert (storage/key).read_bytes()==snapshot
    finally:restore('subtitle.translate',old)
    # A deterministic blocked-word review is a successful evaluation with an
    # explicit negative decision. No LLM, submission or review bypass.
    t=task('ready_for_review');old=setting('publish.review.settings')
    try:
        db("INSERT INTO app_settings(key,value_json,version) VALUES('publish.review.settings',%s,1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",(json.dumps({'enabled':True,'blocked_words':['v7-blocked'],'ai_rules':''}),))
        p=f'/tasks/{t}/actions/publish_review';body={'meta':{'title':'v7-blocked title','desc':'fixture','typeid':180,'tags':['fixture'],'copyright':1}}
        r=check('/tasks/{task_id}/actions/publish_review',p,body,lambda x:x['checked'] is True and x['ok'] is False and x['matched_blocked_words']==['v7-blocked'])
        saved=db('SELECT publish_review_approved,publish_review_result,publish_review_fingerprint FROM tasks WHERE id=%s',(t,),True)
        assert saved[0] is False and saved[1]==r and len(saved[2])==64
        assert db('SELECT count(*) FROM publish_jobs WHERE task_id=%s',(t,),True)==(0,)
    finally:restore('publish.review.settings',old)
    # Sidecar account checks/imports use the real proxy and authenticated
    # loopback test double. No actual account cookies are used.
    account='1e2baeba-d95b-44dd-bf7b-aedb0ce5cd7a'
    check('/settings/publish/social/accounts/{account_id}/check','/settings/publish/social/accounts/'+account+'/check',{},lambda x:x['check_job_id']=='mock' and x['account']['id']==account and x['account']['check_state']=='valid')
    data=b'--vr2contract\r\nContent-Disposition: form-data; name="account_name"\r\n\r\nContractV7\r\n--vr2contract\r\nContent-Disposition: form-data; name="file"; filename="storage_state.json"\r\nContent-Type: application/json\r\n\r\n{"cookies":[],"origins":[]}\r\n--vr2contract--\r\n'
    check('/settings/publish/social/accounts/{platform}','/settings/publish/social/accounts/douyin',data,lambda x:x['check_job_id']=='mock' and x['account']['platform']=='douyin' and x['account']['check_state']=='pending',form=True)
    print(f'PASS: {len(records)} independent task action and account proxy assertions',flush=True)
    return records
