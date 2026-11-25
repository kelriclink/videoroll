"""Standalone subtitle and Bilibili service positive fixtures (loopback only)."""
import io,json,os,threading,uuid,zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import psycopg

class Backend(BaseHTTPRequestHandler):
    def reply(self,code,obj):
        raw=json.dumps(obj,ensure_ascii=False).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if 'SESSDATA=contract-v5-cookie' not in self.headers.get('Cookie',''):return self.reply(403,{'code':-101})
        if urlsplit(self.path).path=='/x/member/web/account':return self.reply(200,{'code':0,'data':{'mid':617,'uname':'contract owner'}})
        if urlsplit(self.path).path=='/x/vupre/web/archive/pre':return self.reply(200,{'code':0,'data':{'typelist':[{'id':17,'name':'科技','children':[{'id':180,'name':'知识'}]}]}})
        return self.reply(404,{'code':-404})
    def do_POST(self):
        if self.path!='/v1/chat/completions':return self.reply(403,{'error':'no posting or upload in this fixture'})
        req=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert req['model']=='contract-v5-model' and self.headers.get('Authorization')=='Bearer contract-v5-key'
        prompt=req['messages'][-1]['content']
        if '候选分区' in prompt:
            assert '180' in prompt
            answer={'typeid':180,'reason':'fixture category'}
        else:
            assert '请翻译下面这段文本' in prompt
            answer={'translation':'测试翻译'}
        self.reply(200,{'choices':[{'message':{'content':json.dumps(answer,ensure_ascii=False)}}],'usage':{'prompt_tokens':30,'completion_tokens':10}})
    def log_message(self,*args):pass

class Fixtures:
    def __init__(self,root):
        self.root=root;self.url=os.environ['DATABASE_URL'];u=urlsplit(self.url)
        assert u.hostname=='127.0.0.1' and u.path=='/videoroll_test'
        assert os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']=='v2-local-test-only-0123456789abcdef'
        assert os.environ['VIDEOROLL_V2_ALLOW_TEST_BILIBILI_LOOPBACK']=='true'
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Backend)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.saved={}
    def configure(self,service,env):
        if service=='bilibili_publisher':
            env.update(VIDEOROLL_V2_TEST_BILIBILI_BASE=self.base+'/',VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI='true')
        if service in ('bilibili_publisher','subtitle_service'):
            with psycopg.connect(self.url) as db:
                values={'subtitle.translate':{'default_provider':'openai','openai':{'base_url':self.base+'/v1','model':'contract-v5-model','api_key_enc':'dbplain:v1:contract-v5-key'}},'subtitle.auto_profile':{'translate_provider':'openai','translate_model_provider_id':'legacy'}}
                if service=='bilibili_publisher':values.update({'bilibili.auth':{'cookie_enc':'dbplain:v1:SESSDATA=contract-v5-cookie; bili_jct=1234567890123456'},'publish.platforms':{'bilibili':True},'publish.review.settings':{'enabled':False,'blocked_words':[],'ai_rules':''}})
                for key,value in values.items():
                    self.saved[key]=db.execute('SELECT value_json,version FROM app_settings WHERE key=%s',(key,)).fetchone()
                    db.execute('INSERT INTO app_settings(key,value_json,version) VALUES(%s,%s,1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json',(key,json.dumps(value)))
    def restore(self):
        with psycopg.connect(self.url) as db:
            for key,row in self.saved.items():
                if row is None:db.execute('DELETE FROM app_settings WHERE key=%s',(key,))
                else:db.execute('UPDATE app_settings SET value_json=%s,version=%s WHERE key=%s',(json.dumps(row[0]),row[1],key))
        self.saved.clear()
    def close(self):
        self.restore();self.server.shutdown();self.server.server_close()
    def probe(self,service,call):
        results=[]
        def check(method,path,body=None,actual=None,form=False,verify=lambda x:True):
            dest=actual or path
            deny=call(method,dest,body,auth=False,form=form)[0]
            assert deny in (401,403),(path,deny)
            code,mime,obj=call(method,dest,body,form=form)
            assert code==200 and verify(obj),(service,path,code,str(obj)[:250])
            results.append(dict(service=service,method=method,path=path,http_status=code,response_kind=mime,json_fields=sorted(obj),probe='positive',issue=None,fixture_asserted=True,unauthorized_status=deny))
            return obj
        if service=='subtitle_service':
            assert os.environ['VIDEOROLL_V2_TEST_CLOUDFLARE_BASE']=='http://127.0.0.1:18091'
            assert os.environ['VIDEOROLL_V2_TEST_HF_BASE']=='http://127.0.0.1:18090'
            check('POST','/subtitle/asr/external/test',{'base_url':'http://127.0.0.1:18091/v1','api_key':'external-fixture','model':'whisper-test','batch_size':3,'vad_enabled':False,'speech_pad_ms':0,'vad_threshold':0.6},verify=lambda x:x.get('ok') and x.get('text')=='External test success' and x['segments']==1)
            check('POST','/subtitle/asr/cloudflare/test',{'account_id':'testacct','model':'@cf/openai/whisper-large-v3-turbo','api_key':'cf-fixture'},verify=lambda x:x.get('ok') and x.get('text')=='Cloudflare test success' and x['segments']==1)
            check('POST','/subtitle/translate/test',{'text':'Printed circuit boards','target_lang':'zh','style':'口语自然'},verify=lambda x:x=={'translated_text':'测试翻译'})
            model_root=Path(os.environ['SUBTITLE_WHISPER_MODEL_DIR']);assert model_root==self.root/'target/v2-smoke-whisper-models'
            name='contract-v5-download-'+uuid.uuid4().hex[:8]
            check('POST','/subtitle/models/download',{'engine':'faster-whisper','model':'tiny','name':name,'revision':'main'},verify=lambda x:x['name']==name and x['path']==str(model_root/name))
            assert (model_root/name/'config.json').read_bytes()==b'{"model":"tiny","revision":"main"}'
            assert (model_root/name/'weights/model.bin').read_bytes()==bytes(range(256))*4
            name='contract-v5-zip-'+uuid.uuid4().hex[:8];blob=io.BytesIO()
            with zipfile.ZipFile(blob,'w') as z:z.writestr('config.json','{}');z.writestr('weights/model.bin',b'fixture weights')
            data=b'--vr2contract\r\nContent-Disposition: form-data; name="file"; filename="fixture.zip"\r\nContent-Type: application/zip\r\n\r\n'+blob.getvalue()+b'\r\n--vr2contract--\r\n'
            check('POST','/subtitle/models/upload',data,actual='/subtitle/models/upload?name='+name,form=True,verify=lambda x:x['name']==name and x['size_bytes']==17)
            assert (model_root/name/'weights/model.bin').read_bytes()==b'fixture weights'
        elif service=='bilibili_publisher':
            check('GET','/bilibili/auth/me',verify=lambda x:x['mid']==617 and x['uname']=='contract owner')
            check('GET','/bilibili/archive/types',verify=lambda x:x['typelist'][0]['children'][0]['id']==180)
            check('POST','/bilibili/archive/type/recommend',{'text':'contract category'},verify=lambda x:x['ok'] and x['typeid']==180 and x['used_text']=='contract category')
            t=str(uuid.uuid4());key=f'v2/contract-bili/{t}/source.mp4';p=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT'])/key
            p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((self.root/'target/contract-v3-media.mp4').read_bytes())
            with psycopg.connect(self.url) as db:
                db.execute("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(%s,'youtube','https://youtu.be/0123456789A','authorized','rendered','rust')",(t,))
                db.execute("INSERT INTO assets(id,task_id,kind,storage_key) VALUES(%s,%s,'video_final',%s)",(uuid.uuid4(),t,key))
            body={'task_id':t,'video':{'type':'storage','key':key},'typeid_mode':'meta','meta':{'title':'contract v5 publish intent','desc':'fixture','typeid':180,'tags':['fixture'],'copyright':2,'source':'https://youtu.be/0123456789A'}}
            result=check('POST','/bilibili/publish',body,verify=lambda x:x['state']=='submitting')
            assert call('POST','/bilibili/publish',body)[2]==result
            with psycopg.connect(self.url) as db:
                assert db.execute('SELECT task_id::text,state::text FROM publish_jobs WHERE id=%s',(result['job_id'],)).fetchone()==(t,'submitting')
                assert db.execute("SELECT count(*) FROM operation_inbox WHERE task_id=%s AND stage='publish'",(t,)).fetchone()==(1,)
                assert db.execute('SELECT status FROM operation_inbox WHERE operation_key=%s',('publish-job:'+result['job_id'],)).fetchone()==('processing',)
        return results
