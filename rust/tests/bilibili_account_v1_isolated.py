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
from videoroll.apps.bilibili_publisher.schemas import BilibiliAuthSettingsRead, BilibiliArchiveTypesRead, BilibiliMeRead, PublishJobRead
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
    server.shutdown();server.server_close();thread.join(timeout=2)
