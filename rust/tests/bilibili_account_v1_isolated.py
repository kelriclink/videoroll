"""Isolated original-table Bilibili auth/types/job read differential; no real Bilibili traffic."""
import json, os, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from videoroll.apps.bilibili_publisher.auth_settings_store import get_bilibili_auth_settings, update_bilibili_auth_settings
from videoroll.apps.bilibili_publisher.schemas import BilibiliAuthSettingsRead, BilibiliArchiveTypesRead, BilibiliMeRead, PublishJobRead, BilibiliTypeRecommendResponse
from videoroll.apps.bilibili_publisher.typeid_recommender import flatten_typelist
from videoroll.db.models import PublishJob

uri = os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in uri and uri.endswith('/videoroll_test')
assert os.environ.get('VIDEOROLL_V2_ALLOW_TEST_BILIBILI_LOOPBACK')=='true'
engine=create_engine(uri)
base=os.environ.get('VIDEOROLL_V2_TEST_HTTP_BASE','http://127.0.0.1:18080')+'/api/v1'
hdr={'x-videoroll-v2-internal-token':os.environ['VIDEOROLL_V2_INTERNAL_TOKEN'],
     'content-type':'application/json'}
def call(path, method='GET', payload=None):
    req=Request(base+path,method=method,data=(json.dumps(payload).encode() if payload is not None else None),headers=hdr)
    try:r=urlopen(req,timeout=12)
    except HTTPError as e:r=e
    with r:
        raw=r.read()
        try:obj=json.loads(raw)
        except ValueError:obj=raw.decode(errors='replace')
        return r.status,obj

observed=[]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        observed.append((self.path,self.headers.get('Cookie')))
        if self.path.startswith('/x/member/web/account'):
            obj={'code':0,'data':{'mid':117,'uname':'video owner','userid':'creator','sign':'hello','rank':100}}
        elif self.path.startswith('/x/vupre/web/archive/pre?ts='):
            obj={'code':0,'data':{'typelist':[{'id':17,'name':'科技','children':[{'id':180,'name':'知识','visible':True}],'extra':'kept'}]}}
        else:
            self.send_response(404);self.end_headers();return
        body=json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('content-type','application/json')
        self.send_header('content-length',str(len(body)))
        self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass

server=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('VIDEOROLL_V2_TEST_BILIBILI_PORT','18096'))),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
model_requests=[]
class ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.path=='/v1/chat/completions',self.path
        length=int(self.headers.get('Content-Length','0'))
        assert 0<length<160000,length
        req=json.loads(self.rfile.read(length))
        prompt=req['messages'][-1]['content']
        if req['model']=='Index-Translate-35B-A3B':
            assert 'Authorization' not in self.headers
            assert req['stream'] is False and '请将以下文本翻译' in prompt,req
            answer='来自Index的译文'
        elif '请翻译下面这段文本' in prompt:
            assert self.headers.get('Authorization')=='Bearer isolated-typeid-key'
            assert req['model']=='mock-bili-typeid' and req['response_format']['type']=='json_object',req
            assert '目标语言：zh' in prompt
            answer=json.dumps({'translation':'来自OpenAI的译文'},ensure_ascii=False)
        else:
            assert self.headers.get('Authorization')=='Bearer isolated-typeid-key'
            assert req['model']=='mock-bili-typeid' and req['response_format']['type']=='json_object',req
            assert '180\t科技/知识' in prompt and '17\t科技' not in prompt,prompt
            model_requests.append(prompt)
            tid=999999 if 'hallucinate category' in prompt else 180
            answer=json.dumps({'typeid':tid,'reason':'电子科技知识'},ensure_ascii=False)
        raw=json.dumps({'choices':[{'message':{'content':answer}}],
                        'usage':{'prompt_tokens':50,'completion_tokens':15}}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def log_message(self,*_args):pass
model_server=ThreadingHTTPServer(('127.0.0.1',int(os.environ['VIDEOROLL_V2_TEST_TRANSLATE_PORT'])),ModelHandler)
model_thread=threading.Thread(target=model_server.serve_forever,daemon=True);model_thread.start()
try:
    status,v=call('/bilibili/auth/settings')
    assert status==200 and v=={'cookie_set':False,'sessdata_set':False,'bili_jct_set':False},(status,v)
    status,v=call('/bilibili/archive/types')
    assert status==400 and 'cookie' in v['detail'],(status,v)
    with Session(engine) as db:
        expected=update_bilibili_auth_settings(db,{'cookie':'Cookie: SESSDATA=previous; bili_jct=existing'})
    status,v=call('/bilibili/auth/settings')
    assert status==200 and v==BilibiliAuthSettingsRead(**expected).model_dump(),(status,v,expected)
    status,v=call('/bilibili/auth/settings','PUT',{'cookie':'Cookie: SESSDATA=abc; bili_jct=xyz'})
    assert status==200 and v=={'cookie_set':True,'sessdata_set':True,'bili_jct_set':True},(status,v)
    with Session(engine) as db:
        assert v==get_bilibili_auth_settings(db)
        persisted=db.execute(text("SELECT value_json FROM app_settings WHERE key='bilibili.auth'")).scalar_one()
        assert persisted['cookie_enc']=='dbplain:v1:SESSDATA=abc; bili_jct=xyz'
    assert 'abc' not in json.dumps(v) and 'xyz' not in json.dumps(v)
    status,v=call('/bilibili/auth/me')
    expected=BilibiliMeRead(mid=117,uname='video owner',userid='creator',sign='hello',rank='100').model_dump()
    assert status==200 and v==expected,(status,v,expected)
    status,v=call('/bilibili/archive/types')
    expected=BilibiliArchiveTypesRead(typelist=[{'id':17,'name':'科技','children':[{'id':180,'name':'知识','visible':True}],'extra':'kept'}]).model_dump()
    assert status==200 and v==expected,(status,v,expected)
    assert observed and all(cookie=='SESSDATA=abc; bili_jct=xyz' for _,cookie in observed),observed
    assert flatten_typelist(expected['typelist'])==[{'id':180,'name':'知识','path':'科技/知识'}]
    # Original translation configuration is the source of truth for the model;
    # no production OpenAI key, platform account or external network is used.
    provider_config={'default_provider':'openai','openai':{
        'base_url':f'http://127.0.0.1:{model_server.server_address[1]}/v1',
        'model':'mock-bili-typeid','api_key_enc':'dbplain:v1:isolated-typeid-key'}}
    with engine.begin() as db:
        db.execute(text("""INSERT INTO app_settings(key,value_json,version)
              VALUES('subtitle.translate',CAST(:v AS jsonb),1)
              ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"""),{'v':json.dumps(provider_config)})
        db.execute(text("""INSERT INTO app_settings(key,value_json,version)
              VALUES('subtitle.auto_profile','{"translate_provider":"openai","translate_model_provider_id":"legacy"}'::jsonb,1)
              ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"""))
    status,result=call('/bilibili/archive/type/recommend','POST',{'text':'电路板走线为什么不同宽度？'})
    expected_type=BilibiliTypeRecommendResponse(ok=True,typeid=180,path='科技/知识',reason='电子科技知识',used_text='电路板走线为什么不同宽度？').model_dump()
    assert status==200 and result==expected_type,(status,result,expected_type)
    status,invalid=call('/bilibili/archive/type/recommend','POST',{'text':'hallucinate category'})
    assert status==200 and invalid['ok']==False and invalid['typeid'] is None, (status,invalid)
    assert invalid['reason']=='typeid not in candidate list',invalid
    assert len(model_requests)==2,model_requests
    status,translated=call('/subtitle/translate/test','POST',{
        'text':'Printed circuit boards','target_lang':'zh','style':'口语自然'})
    assert status==200 and translated=={'translated_text':'来自OpenAI的译文'},(status,translated)
    for bad in ('', 'x'*2001):
        status,rejected=call('/subtitle/translate/test','POST',{'text':bad})
        assert status==400,(status,rejected)
    status,default=call('/subtitle/translate/test','POST',{})
    assert status==200 and default['translated_text']=='来自OpenAI的译文',(status,default)
    status,invalid=call('/bilibili/archive/type/recommend','POST',{'text':'  '})
    assert status==400 and 'missing text' in invalid['detail'],(status,invalid)
    with engine.begin() as db:
        db.execute(text("""INSERT INTO app_settings(key,value_json,version)
             VALUES('bilibili.tags.00000000-0000-0000-0000-000000000001','{"summary":"摘要应优先于请求文本"}'::jsonb,1)
             ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"""))
    status,picked=call('/bilibili/archive/type/recommend','POST',{
        'task_id':'00000000-0000-0000-0000-000000000001','text':'fallback should not be used'})
    assert status==200 and picked['used_text']=='摘要应优先于请求文本',(status,picked)
    assert '摘要应优先于请求文本' in model_requests[-1] and 'fallback should not be used' not in model_requests[-1]
    with engine.begin() as db:
        db.execute(text("""UPDATE app_settings SET value_json='{"translate_provider":"index_translate"}'::jsonb
            WHERE key='subtitle.auto_profile'"""))
    status,rejected=call('/bilibili/archive/type/recommend','POST',{'text':'not openai'})
    assert status==400 and '不支持' in rejected['detail'],(status,rejected)
    assert len(model_requests)==3
    status,index_translation=call('/subtitle/translate/test','POST',{
        'text':'Printed circuit boards','target_lang':'zh','style':'口语自然'})
    assert status==200 and index_translation=={'translated_text':'来自Index的译文'},(status,index_translation)
    with engine.begin() as db:
        db.execute(text("""UPDATE app_settings SET value_json='{"translate_provider":"openai","translate_model_provider_id":"legacy"}'::jsonb
            WHERE key='subtitle.auto_profile'"""))
    print('PASS DIFF: native Subtitle translation test with original OpenAI/Index provider selection, real loopback HTTP, empty and oversized input rejection')
    print('PASS DIFF: native Bilibili type recommendation uses original translation model, actual mocked partition IDs, refuses hallucinated IDs, respects task summary and provider selection')
    task,job=uuid4(),uuid4()
    with engine.begin() as db:
        db.execute(text("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner)
            VALUES(CAST(:task AS uuid),'youtube','authorized','created','rust')"""),{'task':str(task)})
        db.execute(text("""INSERT INTO publish_jobs(id,task_id,platform,state,bvid,aid,meta_json)
            VALUES(CAST(:job AS uuid),CAST(:task AS uuid),'bilibili','draft','BV1TEST','123','{}'::jsonb)"""),
            {'job':str(job),'task':str(task)})
    status,v=call(f'/bilibili/publish/jobs/{job}')
    with Session(engine) as db:
        py=PublishJobRead.model_validate(db.get(PublishJob,job)).model_dump(mode='json')
    assert status==200,(status,v)
    for key in ['id','task_id','state','aid','bvid']:
        assert v[key]==py[key],(key,v,py)
    for key in ['created_at','updated_at']:
        assert datetime.fromisoformat(v[key].replace('Z','+00:00'))==datetime.fromisoformat(py[key].replace('Z','+00:00'))
    status,v=call(f'/bilibili/publish/jobs/{uuid4()}')
    assert status==404,(status,v)
    status,v=call('/bilibili/auth/settings','PUT',{'cookie':''})
    assert status==200 and v=={'cookie_set':False,'sessdata_set':False,'bili_jct_set':False},(status,v)
    status,v=call('/bilibili/auth/me')
    assert status==400,(status,v)
    print('PASS DIFF: original Bilibili auth settings GET/PUT, retained dbplain credentials, mock HTTP me/archive types, exact publish_jobs DTO, missing jobs and missing cookie')
finally:
    model_server.shutdown();model_server.server_close();model_thread.join(timeout=2)
    server.shutdown();server.server_close();thread.join(timeout=2)
