"""Disposable local Bilibili creator + UPOS simulator for Rust V1 publish parity."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlparse
import json,time
NOW=int(time.time())
ACTIVITIES=[
 {'id':999,'sid':888,'name':'魔兽世界活动','state':1,'stime':NOW-86400,'etime':NOW+86400},
 {'id':998,'name':'魔兽世界报名活动','state':1,'stime':NOW-86400,'etime':NOW+86400},
 {'id':997,'name':'魔兽世界过期活动','state':1,'stime':NOW-86400,'etime':NOW-1}]
TOPICS=[
 {'topic_id':301,'mission_id':901,'topic_name':'魔兽世界活动','act_protocol':'允许转载作品参与，面向全部视频创作者'},
 {'topic_id':302,'mission_id':902,'topic_name':'魔兽世界普通话题','description':'普通话题'},
 {'topic_id':303,'mission_id':903,'topic_name':'魔兽世界报名活动','act_protocol':'允许转载，但是需要报名审核'}]

STATE={'preupload':0,'uploads':0,'chunks':0,'complete':0,'predict':0,'submit':0,'ambiguous_submit':0,'cover':0,'ai':0,'ai_invalid':0,'type_catalog':0,'activity_list':0,'activity_detail':0,'topic_search':0,'activity_shortlist':0,'activity_final':0}
class Handler(BaseHTTPRequestHandler):
 def send(self,code,body,etag=None):
  value=json.dumps(body).encode()
  self.send_response(code)
  self.send_header('Content-Type','application/json')
  if etag:self.send_header('ETag',etag)
  self.send_header('Content-Length',str(len(value)))
  self.end_headers();self.wfile.write(value)
 def auth(self):return any('SESSDATA='+item in self.headers.get('Cookie','') for item in ('video-roll-test','isolated-account-A','isolated-account-B','isolated-rotated'))
 def do_GET(self):
  url=urlparse(self.path)
  if url.path=='/__state':return self.send(200,STATE)
  if url.path=='/x/activity/page/list':
   assert not self.headers.get('Cookie')
   STATE['activity_list']+=1
   return self.send(200,{'code':0,'data':{'list':ACTIVITIES}})
  if url.path=='/x/activity/subject/info':
   assert not self.headers.get('Cookie') and parse_qs(url.query)['sid']==['888']
   STATE['activity_detail']+=1
   return self.send(200,{'code':0,'data':{**ACTIVITIES[0],'id':888,'dic':'活动允许转载作品参与'}})
  if not self.auth():return self.send(403,{'code':-101})
  if url.path=='/x/vupre/web/topic/search':
   STATE['topic_search']+=1
   return self.send(200,{'code':0,'data':{'result':{'topics':TOPICS}}})
  if url.path=='/x/vupre/web/topic/type':
   return self.send(200,{'code':0,'data':{'topics':TOPICS}})
  if url.path=='/x/member/web/account':
   cookie=self.headers.get('Cookie','')
   mid=61001 if 'isolated-account-A' in cookie else 61002 if 'isolated-account-B' in cookie else 61000
   return self.send(200,{'code':0,'data':{'mid':mid,'uname':'isolated-user-'+str(mid)}})
  if url.path=='/x/vupre/web/archive/pre':
   STATE['type_catalog']+=1
   return self.send(200,{'code':0,'data':{'typelist':[
     {'id':17,'name':'科技','children':[{'id':172,'name':'知识'},{'id':180,'name':'科学科普'}]},
     {'id':129,'name':'其他'}]}})
  if url.path=='/preupload':
   STATE['preupload']+=1
   name=parse_qs(url.query).get('name',[''])[0]
   second=name=='dub.mp4'
   return self.send(200,{'OK':1,'auth':'isolated-upos','endpoint':'//127.0.0.1:18199',
     'upos_uri':'upos://bucket/rust-dub.mp4' if second else 'upos://bucket/rust-test.mp4',
     'biz_id':422 if second else 421,'chunk_size':4096})
  return self.send(404,{'code':-404})
 def do_POST(self):
  url=urlparse(self.path)
  if url.path.startswith('/bucket/'):
   if self.headers.get('x-upos-auth')!='isolated-upos':return self.send(403,{'OK':0})
   length=int(self.headers.get('Content-Length') or 0)
   if length:self.rfile.read(length)
   if 'uploads' in url.query:
    STATE['uploads']+=1
    return self.send(200,{'OK':1,'upload_id':'isolated-upload-v1'})
   STATE['complete']+=1
   return self.send(200,{'OK':1})
  if url.path=='/v1/chat/completions':
   size=int(self.headers.get('Content-Length','0'))
   if size<1 or size>256000:return self.send(413,{'error':'payload too large'})
   body=json.loads(self.rfile.read(size))
   assert body['model']=='mock-bilibili-ai-summary',body.get('model')
   assert body['response_format']=={'type':'json_object'},body
   text=body['messages'][-1]['content']
   system=body['messages'][0]['content']
   if '话题及活动筛选助手' in system or '已核实的ref' in system:
    payload=json.loads(text)
    title=payload['video']['title']
    if '话题及活动筛选助手' in system:
     STATE['activity_shortlist']+=1
     refs={c['ref'] for c in payload['candidates']}
     assert 'public:999' in refs and 'public:998' in refs and 'public:997' not in refs,refs
     if '发现失败' in title:return self.send(503,{'error':'simulated AI outage'})
     selected={'refs':['public:999','public:998','topic:302']}
    else:
     STATE['activity_final']+=1
     refs={c['ref'] for c in payload['details']}
     assert refs=={'public:999','topic:302'},refs
     selected={'ref':'public:777' if '伪造' in title else 'topic:302' if '普通话题' in title else 'public:999','reason':'fixture'}
    return self.send(200,{'choices':[{'message':{'content':json.dumps(selected)}}],'usage':{'prompt_tokens':16,'completion_tokens':10,'total_tokens':26}})
   assert '候选分区' in text and '172' in text and '180' in text,text
   STATE['ai']+=1
   invalid='ai-invalid' in text
   if invalid:STATE['ai_invalid']+=1
   else:assert 'unique-summarized-ai-source' in text and 'ai-summary' not in text,text
   content=json.dumps({'typeid':999999 if invalid else 172,'reason':'isolated mock choice'},ensure_ascii=False)
   return self.send(200,{'id':'chatcmpl-fixture','object':'chat.completion','choices':[
    {'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],
    'usage':{'prompt_tokens':16,'completion_tokens':10,'total_tokens':26}})
  if not self.auth():return self.send(403,{'code':-101})
  size=int(self.headers.get('Content-Length','0'))
  if size<1 or size>10*1024*1024:return self.send(413,{'code':-413})
  body=self.rfile.read(size)
  if url.path=='/x/vupre/web/archive/types/predict':
   assert b'upload_id' in body and b'isolated-upload-v1' in body,body[:120]
   STATE['predict']+=1
   return self.send(200,{'code':0,'data':[{'id':180,'name':'科技'}]})
  if url.path=='/x/vu/web/cover/up':
   assert b'data%3Aimage' in body or b'data:image' in body
   STATE['cover']+=1
   return self.send(200,{'code':0,'data':{'url':'https://i0.hdslb.com/mock_cover.jpg'}})
  if url.path=='/x/vu/web/add/v3':
   data=json.loads(body)
   expected={'named-account-A':('isolated-account-A','ABCD123456789012'),
             'named-account-B':('isolated-account-B','BCDE123456789012')}.get(data['title'],('video-roll-test','1234567890123456'))
   assert 'SESSDATA='+expected[0] in self.headers.get('Cookie',''),data['title']
   assert data['csrf']==expected[1],data['title']
   assert data['title'] in ['isolated Rust P1','ambiguous-publish','two-part','【熟肉&中配】two-part','ai-summary','ai-invalid','batch-only','batch-unknown','named-account-A','named-account-B','魔兽世界活动验收','魔兽世界普通话题验收','魔兽世界AI伪造验收','魔兽世界发现失败验收','魔兽世界人工指定验收'],data
   assert data['copyright']==2 and data['source'].startswith('https://youtu.be/'),data
   assert data['videos'][0]['filename']=='rust-test' and data['videos'][0]['cid']==421,data
   if data['title']=='isolated Rust P1':assert data['cover']=='https://i0.hdslb.com/mock_cover.jpg',data
   if data['title']=='two-part':
    assert data['videos']==[
      {'filename':'rust-test','title':'P1中文字幕版','desc':'','cid':421},
      {'filename':'rust-dub','title':'P2中文配音版','desc':'','cid':422}],data
   elif data['title']=='【熟肉&中配】two-part':
    assert data['videos']==[
      {'filename':'rust-test','title':'two-part','desc':'','cid':421},
      {'filename':'rust-dub','title':'【中配】two-part','desc':'','cid':422}],data
   else:
    assert len(data['videos'])==1,data
   expected_tid={'isolated Rust P1':180,'ai-summary':172,'ai-invalid':180}.get(data['title'],171)
   assert data['tid']==expected_tid,data
   if data['title'].startswith('魔兽世界'):
    expected_ids={'魔兽世界活动验收':(301,901),'魔兽世界普通话题验收':(302,None),'魔兽世界人工指定验收':(302,None)}.get(data['title'],(None,None))
    assert (data.get('topic_id'),data.get('mission_id'))==expected_ids,data
   STATE['submit']+=1
   if data['title'] in ('ambiguous-publish','batch-unknown'):
    STATE['ambiguous_submit']+=1
    return self.send(503,{'code':-500,'message':'archive may have been accepted'})
   return self.send(200,{'code':0,'data':{'aid':56321,'bvid':'BV1RUSTMOCK1'}})
  return self.send(404,{'code':-404})
 def do_PUT(self):
  url=urlparse(self.path)
  if not url.path.startswith('/bucket/') or self.headers.get('x-upos-auth')!='isolated-upos':
   return self.send(403,{'OK':0})
  size=int(self.headers.get('Content-Length','0'))
  if size<1 or size>4096:return self.send(400,{'OK':0})
  body=self.rfile.read(size)
  if not body:return self.send(400,{'OK':0})
  STATE['chunks']+=1
  return self.send(200,{'OK':1},etag='"isolated-chunk-etag"')
 def log_message(self,*args):pass
ThreadingHTTPServer(('127.0.0.1',18199),Handler).serve_forever()
