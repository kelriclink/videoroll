"""Disposable local Bilibili creator + UPOS simulator for Rust V1 publish parity."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlparse
import json

STATE={'preupload':0,'uploads':0,'chunks':0,'complete':0,'predict':0,'submit':0,'ambiguous_submit':0,'cover':0}
class Handler(BaseHTTPRequestHandler):
 def send(self,code,body,etag=None):
  value=json.dumps(body).encode()
  self.send_response(code)
  self.send_header('Content-Type','application/json')
  if etag:self.send_header('ETag',etag)
  self.send_header('Content-Length',str(len(value)))
  self.end_headers();self.wfile.write(value)
 def auth(self):return 'SESSDATA=video-roll-test' in self.headers.get('Cookie','')
 def do_GET(self):
  url=urlparse(self.path)
  if url.path=='/__state':return self.send(200,STATE)
  if not self.auth():return self.send(403,{'code':-101})
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
   assert data['csrf']=='1234567890123456',data
   assert data['title'] in ['isolated Rust P1','ambiguous-publish','two-part'],data
   assert data['copyright']==2 and data['source'].startswith('https://youtu.be/'),data
   assert data['videos'][0]['filename']=='rust-test' and data['videos'][0]['cid']==421,data
   if data['title']=='isolated Rust P1':assert data['cover']=='https://i0.hdslb.com/mock_cover.jpg',data
   if data['title']=='two-part':
    assert data['videos']==[
      {'filename':'rust-test','title':'P1中文字幕版','desc':'','cid':421},
      {'filename':'rust-dub','title':'P2中文配音版','desc':'','cid':422}],data
   else:
    assert len(data['videos'])==1,data
   assert data['tid']==(180 if data['title']=='isolated Rust P1' else 171),data
   STATE['submit']+=1
   if data['title']=='ambiguous-publish':
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
