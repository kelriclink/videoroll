#!/usr/bin/env python3
"""Authenticated YouTube home Browse mock (never requests real YouTube)."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

def video(uid,title,length=None,short=False):
    if short:
        return {"reelItemRenderer":{"videoId":uid,"title":{"simpleText":title},
               "thumbnailOverlays":[{"thumbnailOverlayTimeStatusRenderer":{"style":"SHORTS"}}]}}
    return {"videoRenderer":{"videoId":uid,"title":{"simpleText":title},
        **({"lengthText":{"simpleText":length}} if length else {})}}

device=["" for _ in range(108)]
device[13]="isolated-visitor"
device[16]="2.20261124.00.00"
device[17]="Windows"
device[18]="11.0"
SW=")]}'\n"+json.dumps([[None,None,[[device],"AIzaYoutubeMockKey"]]])
FAIL_BROWSE=False

FIRST={"contents":{
    "richGridRenderer":{"contents":[
        video("ABCDEF12345","Already ingested","12:35"),
        video("SHRTID12345","Shorts 40 sec","0:40",True),
        video("UNKNOWN1234","Unknown duration"),
        video("SHORTD12345","Under minimum","1:05"),
        video("FRESHXX1234","Qualified long","10:02"),
        {"continuationItemRenderer":{"continuationEndpoint":{"continuationCommand":{"token":"page2"}}}}
    ]}}}
SECOND={"continuationContents":{"richGridContinuation":{"contents":[
    video("LATERXX1234","Second page long","20:00"),
    video("FRESHXX1234","Repeated video","10:02")
]}}}

class Handler(BaseHTTPRequestHandler):
    def send(self,status,payload,ctype="application/json"):
        body=payload.encode() if isinstance(payload,str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def auth(self):
        value=self.headers.get("Cookie","")
        return "SAPISID=isolated-home-token" in value and "VISITOR_INFO1_LIVE=isolated-visitor" in value
    def do_GET(self):
        if urlparse(self.path).path=="/sw.js_data":
            return self.send(200,SW,"text/plain")
        return self.send(404,{"error":"unrecognized route"})
    def do_POST(self):
        global FAIL_BROWSE
        path=urlparse(self.path)
        if path.path=="/mock/enable-failure":
            FAIL_BROWSE=True
            return self.send(200,{"enabled":True})
        if path.path=="/mock/disable-failure":
            FAIL_BROWSE=False
            return self.send(200,{"enabled":False})
        if path.path!="/youtubei/v1/browse":
            return self.send(404,{"error":"wrong path"})
        if FAIL_BROWSE:
            return self.send(503,{"error":"simulated YouTube outage"})
        if not self.auth():
            return self.send(403,{"error":"missing test Youtube credentials"})
        if self.headers.get("X-Youtube-Client-Name")!="1":
            return self.send(400,{"error":"missing client context"})
        query=parse_qs(path.query)
        if query.get("key")!=["AIzaYoutubeMockKey"]:
            return self.send(400,{"error":"wrong dynamic Innertube key"})
        size=int(self.headers.get("Content-Length","0"))
        try:data=json.loads(self.rfile.read(size))
        except ValueError:return self.send(400,{"error":"invalid JSON"})
        if data.get("context",{}).get("client",{}).get("clientVersion")!="2.20261124.00.00":
            return self.send(400,{"error":"wrong dynamic clientVersion"})
        token=data.get("continuation")
        if token=="page2":return self.send(200,SECOND)
        if data.get("browseId")=="FEwhat_to_watch":return self.send(200,FIRST)
        return self.send(400,{"error":"invalid browse request"})
    def log_message(self,*_args):pass

if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",18195),Handler).serve_forever()
