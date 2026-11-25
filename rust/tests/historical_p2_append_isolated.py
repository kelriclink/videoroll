"""Historical P1 + recovered P2 evidence on real API, PG and loopback member mock."""
import concurrent.futures, hashlib, json, os, socket, subprocess, threading, time, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
import psycopg
url=os.environ['DATABASE_URL'];assert '127.0.0.1:' in url and url.endswith('/videoroll_test')
root=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT']);token=os.environ['VIDEOROLL_V2_INTERNAL_TOKEN'];assert token.startswith('isolated-')
exe=Path(__file__).resolve().parents[1]/'target/debug/videoroll-v2'
counts={}; lock=threading.Lock(); archives={}
class Member(BaseHTTPRequestHandler):
    def reply(self,status,body):
        b=json.dumps(body).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def log_message(self,*args):pass
    def do_GET(self):
        if 'SESSDATA=video-roll-test' not in self.headers.get('Cookie',''):return self.reply(403,{'code':-101})
        p=urlparse(self.path)
        if p.path!='/x/vupre/web/archive/view':return self.reply(404,{'code':-404})
        aid=int(parse_qs(p.query)['aid'][0]);record=archives[aid]
        with lock:counts[('view',aid)]=counts.get(('view',aid),0)+1
        self.reply(200,{'code':0,'data':{'archive':{'aid':aid,'bvid':record['bvid'],'title':'original title','copyright':2,'tid':171,'tag':'electronics','desc':'original source','topic_id':121,'mission_id':987},'videos':record['videos']}})
    def do_POST(self):
        if 'SESSDATA=video-roll-test' not in self.headers.get('Cookie',''):return self.reply(403,{'code':-101})
        if urlparse(self.path).path!='/x/vu/web/edit':return self.reply(404,{'code':-404})
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));aid=data['aid'];record=archives[aid]
        assert data['videos']==[record['videos'][0],{'cid':41002,'filename':'mock_p2_file','title':'【中配】Mock P2','desc':''}],data
        assert data['tid']==171 and data['tag']=='electronics' and data['topic_id']==121 and data['mission_id']==987,data
        with lock:counts[('edit',aid)]=counts.get(('edit',aid),0)+1
        record['videos']=data['videos']
        if aid==34002:return self.reply(503,{'code':-503})
        self.reply(200,{'code':0,'data':{'accepted':True}})
server=ThreadingHTTPServer(('127.0.0.1',0),Member);threading.Thread(target=server.serve_forever,daemon=True).start()
env={**os.environ,'VIDEOROLL_V2_NATIVE_OUTBOX_ONLY':'true','VIDEOROLL_V2_RENDER_MANAGEMENT_NATIVE':'true','VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL':'false','VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH':'false','VIDEOROLL_V2_COMPAT_WRITE':'false','VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI':'true','VIDEOROLL_V2_BILIBILI_API_BASE':f'http://127.0.0.1:{server.server_port}','VIDEOROLL_V2_BILIBILI_COOKIE':'SESSDATA=video-roll-test','VIDEOROLL_V2_BILIBILI_CSRF':'1234567890123456','VIDEOROLL_V2_SOURCE_SCANNER':'false'}
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
base=f'http://127.0.0.1:{port}'
def db():return psycopg.connect(url)
def call(method,path,body=None,auth=True):
    h={'Content-Type':'application/json'}
    if auth:h['x-videoroll-v2-internal-token']=token
    try:r=urlopen(Request(base+'/api/v1'+path,method=method,headers=h,data=None if body is None else json.dumps(body).encode()),timeout=10)
    except HTTPError as e:r=e
    with r:return r.status,json.load(r)
def tick():subprocess.run([str(exe),'p2-tick'],env=env,check=True,timeout=45)
media=root/'p2-fixture.mp4';subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=128x96:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','mpeg4','-c:a','aac','-shortest','-y',str(media)],check=True,timeout=30)
bytes_=media.read_bytes();digest=hashlib.sha256(bytes_).hexdigest();size=len(bytes_)
def seed(aid,*,old=None,wrong_cid=False,owner='rust',wrong_asset=False,duplicate_parts=False):
    t,p,d,r,i,a,lease=[uuid.uuid4() for _ in range(7)];bvid=f'BVfixture{aid}'
    key=f'v2/dubbing/{t}/historical-{r}-{lease}.mp4';path=root/key;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(bytes_)
    receipt={'mode':'web','result':{'aid':str(aid),'bvid':bvid},'video_parts':[{'storage_key':f'raw/{t}/p1.mp4','title':'P1 original','upload':{'biz_id':41001}}]}
    if duplicate_parts:receipt['video_parts'].append({'upload':{'biz_id':41003}})
    if old is not None:receipt['auto_dub_p2']=old
    archives[aid]={'bvid':bvid,'videos':[{'cid':41099 if wrong_cid else 41001,'filename':'p1_video','title':'P1 original','desc':'P1'}]}
    with db() as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','published',%s)",(t,owner))
        c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,aid,bvid,meta_json,response_json) VALUES(%s,%s,'bilibili','published',%s,%s,%s,%s)",(p,t,str(aid),bvid,json.dumps({'meta':{'title':'【熟肉】Mock P2'}}),json.dumps(receipt)))
        c.execute("INSERT INTO dubbing_jobs(id,task_id,status,progress,output_json) VALUES(%s,%s,'succeeded',100,%s)",(d,t,json.dumps({'render_job_id':str(r),'video_key':key,'youdub':{'stage':'done'}})))
        c.execute("INSERT INTO render_jobs(id,task_id,status,operation_key) VALUES(%s,%s,'succeeded',%s)",(r,t,f'youdub:p2-render:{d}'))
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_dubbed',%s,%s,%s)",(a,t,key,digest,size))
        c.execute("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES(%s,%s,%s,'live',now()+interval '2 days')",(key,digest,size))
        c.execute("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES(%s,'task',%s,'video_dubbed')",(key,t))
        c.execute("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,status,request_json,result_json,completed_at) VALUES(%s,%s,'rust-historical-p2-render',%s,'completed',%s,%s,now())",(i,f'rust-historical-p2-render:{r}',t,json.dumps({'dubbing_id':str(d),'render_id':str(r)}),json.dumps({'historical_dubbing_id':str(d),'asset_id':str(uuid.uuid4() if wrong_asset else a),'video_key':key,'sha256':digest,'size_bytes':size})))
    return t,p,d,i,key,receipt

def endpoint(item):return f'/tasks/{item[0]}/publish_jobs/{item[1]}'
def enqueue(item,auth=True):return call('POST',endpoint(item)+'/append_p2',{'dubbing_job_id':str(item[2]),'title':'【中配】Mock P2'},auth)
def uploaded(item):
    with db() as c:c.execute("UPDATE vr2_bilibili_p2_appends SET state='uploaded',uploaded_filename='mock_p2_file',uploaded_cid=41002 WHERE publish_job_id=%s",(item[1],))
def verify(item,status):
    t,p,d,i,key,receipt=item
    code,row=call('GET',endpoint(item)+'/p2');assert code==200 and row['state']==status and row['dubbing_job_id']==str(d),row
    with db() as c:
        assert c.execute("SELECT state::text,response_json FROM publish_jobs WHERE id=%s",(p,)).fetchone()==('published',receipt)
        assert c.execute("SELECT runtime_engine,status,p2_dubbing_job_id,p2_state FROM operation_inbox WHERE id=%s",(p,)).fetchone()==('rust-historical-p2-append','completed',i,status)
        assert c.execute("SELECT count(*) FROM pipeline_runs WHERE task_id=%s",(t,)).fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM publish_jobs WHERE task_id=%s",(t,)).fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM storage_object_refs WHERE owner_type='native_bilibili_p2' AND owner_id=%s",(p,)).fetchone()[0]==(1 if status in ('queued','unknown') else 0)
    code,history=call('GET',f'/tasks/{t}/publish_jobs');assert code==200 and next(j for j in history if j['id']==str(p))['auto_dub_p2']['state']==status,history
    code,dubs=call('GET',f'/tasks/{t}/dubbing_jobs');assert code==200 and any(j['id']==str(d) and j['output']['video_key']==key for j in dubs),dubs
proc=subprocess.Popen([str(exe),'api'],env={**env,'VIDEOROLL_V2_BIND':f'127.0.0.1:{port}'})
try:
    for _ in range(100):
        try:
            with urlopen(base+'/ready',timeout=1) as r:
                if r.status==200:break
        except (URLError,ConnectionError):time.sleep(.2)
        assert proc.poll() is None
    else:raise AssertionError('API unavailable')
    good=seed(34001);assert enqueue(good,False)[0]==401
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda _:enqueue(good),range(3)))
    assert all(code==200 and b['state']=='queued' and b['attempt']==1 and b['dubbing_job_id']==str(good[2]) for code,b in results),results
    verify(good,'queued');uploaded(good)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda _:tick(),range(3)))
    verify(good,'published');assert counts.get(('edit',34001))==1;assert enqueue(good)[0]==409
    print('PASS: historical P1 and recovered P2 native enqueue, exact IDs, concurrent edit once, P1 preserved, original UI history',flush=True)
    ambiguous=seed(34002);assert enqueue(ambiguous)[0]==200;uploaded(ambiguous);tick();verify(ambiguous,'unknown');tick();assert counts.get(('edit',34002))==1;assert enqueue(ambiguous)[0]==409
    code,_=call('POST',endpoint(ambiguous)+'/p2/resolve',{'published':False,'evidence':'fixture cannot authorize historical retry'});assert code==409
    code,row=call('POST',endpoint(ambiguous)+'/p2/resolve',{'published':True,'evidence':'fixture owner archive contains the exact recorded CID'});assert code==200 and row['state']=='published',row
    verify(ambiguous,'published');assert counts.get(('edit',34002))==1
    bad=seed(34003,wrong_cid=True);assert enqueue(bad)[0]==200;tick();verify(bad,'failed');assert counts.get(('edit',34003),0)==0
    corrupt=seed(34004);assert enqueue(corrupt)[0]==200;(root/corrupt[4]).write_bytes(b'corrupt');tick();verify(corrupt,'failed');assert counts.get(('view',34004),0)==0
    for item in (seed(34005,old={'state':'unknown'}),seed(34006,old={'state':'published'}),seed(34007,wrong_asset=True),seed(34009,duplicate_parts=True)):
        assert enqueue(item)[0]==409,item
    assert enqueue(seed(34008,owner='python'))[0]==404
    print('PASS: ambiguous edit never replayed; exact owner CID resolves; wrong P1 CID, corrupt media, old unknown/published, wrong asset, ownership and multi-part rejected',flush=True)
finally:
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
    server.shutdown();server.server_close()
