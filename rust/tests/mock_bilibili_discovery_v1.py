#!/usr/bin/env python3
"""Restricted public Bilibili dashboard mock: no outgoing requests."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

lock=threading.Lock()
mode="healthy"
BASE_NOW=int(time.time())
counts={"ranking":0,"popular":0,"activities1":0,"activities2":0}
def ranking():
    return {"code":0,"data":{"list":[
        {"bvid":"BV1AbC2dE3fG","title":"测试视频 A","tname":"科技",
         "owner":{"name":"up主"},"stat":{"view":123,"like":9,"danmaku":2},
         "pic":"//i0.hdslb.com/bfs/archive/test.jpg"},
        {"bvid":"BV1AbC2dE3fG","title":"重复视频"},
        {"bvid":"invalid","title":"Invalid video"},
        {"bvid":"BV9aBc3D4e5F","title":"测试视频 B","tname":"游戏",
         "owner":{"name":"User B"},"stat":{"view":-20,"like":"11","danmaku":3},
         "pic":"https://cdn.attacker.invalid/fake.png"},
    ]}}
def activities(page):
    now=BASE_NOW
    good={"id":101,"state":1,"stime":now-600,"etime":now+86400,
         "name":"魔兽世界活动","desc":"公开活动信息，不代表已报名",
         "pc_url":"https://www.bilibili.com/blackboard/activity-101.html",
         "pc_cover":"//i0.hdslb.com/bfs/banner/test.jpg"}
    if page==1:
        return {"code":0,"data":{"list":[
            {"id":101,"state":0,"stime":now-600,"etime":now+86400,
             "name":"旧活动","pc_url":"https://www.bilibili.com/blackboard/old.html"},
            good,
            {"id":102,"state":1,"stime":now+100,"etime":now+86400,
             "name":"未开始","pc_url":"https://www.bilibili.com/blackboard/later.html"},
            {"id":103,"state":1,"stime":now-600,"etime":now+86400,
             "name":"第三方链接","pc_url":"https://evil.example/blackboard/evil"},
        ]}}
    return {"code":0,"data":{"list":[
        {"id":101,**good,"name":"第二页重复"},
        {"id":104,"state":"1","stime":now-600,"etime":now+86400,
         "name":"明日方舟活动","desc":"任意UP主均可浏览",
         "pc_url":"https://member.bilibili.com/blackboard/activity-104.html",
         "pc_cover":"https://i1.hdslb.com/bfs/banner/104.jpg"}
    ]}}

class Handler(BaseHTTPRequestHandler):
    def respond(self,code,payload):
        encoded=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type","application/json")
        self.send_header("content-length",str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def do_GET(self):
        global mode
        route=urlsplit(self.path)
        if not route.path.startswith("/__mock__/"):
            if self.headers.get("Cookie") or self.headers.get("Authorization") \
                or self.headers.get("Referer")!="https://www.bilibili.com/":
                return self.respond(400,{"detail":"discovery must use public anonymous API"})

        if route.path=="/__mock__/status":
            with lock:self.respond(200,{"mode":mode,"counts":counts.copy()})
            return
        if route.path=="/__mock__/ranking":
            return self.respond(200,ranking())
        if route.path=="/__mock__/activities1":
            return self.respond(200,activities(1))
        if route.path=="/__mock__/activities2":
            return self.respond(200,activities(2))
        if route.path=="/x/web-interface/ranking/v2":
            with lock:counts["ranking"]+=1;state=mode
            if state=="fail":return self.respond(503,{"detail":"blocked upstream"})
            if state=="fallback":return self.respond(200,{"code":-352,"data":{}})
            return self.respond(200,ranking())
        if route.path=="/x/web-interface/popular":
            with lock:counts["popular"]+=1;state=mode
            if state=="fail":return self.respond(503,{"detail":"blocked upstream"})
            return self.respond(200,ranking())
        if route.path=="/x/activity/page/list":
            q=parse_qs(route.query)
            if q.get("plat")!=["1,3"] or q.get("ps")!=["30"]:
                return self.respond(400,{"detail":"wrong fixed params"})
            page=int(q.get("pn",["0"])[0])
            if page not in (1,2):return self.respond(400,{"detail":"invalid page"})
            with lock:counts["activities"+str(page)]+=1;state=mode
            if state=="fail" or (state=="fallback" and page==2):
                return self.respond(503,{"detail":"upstream unavailable"})
            return self.respond(200,activities(page))
        return self.respond(404,{"detail":"unknown mock path"})
    def do_POST(self):
        global mode
        route=urlsplit(self.path)
        if route.path.startswith("/__mock__/mode/"):
            next_mode=route.path.rsplit("/",1)[-1]
            if next_mode not in ("healthy","fallback","fail"):
                return self.respond(400,{"detail":"invalid mock mode"})
            with lock:mode=next_mode
            return self.respond(200,{"mode":mode})
        return self.respond(404,{"detail":"unknown mock control"})
    def log_message(self,*args):pass

if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",18196),Handler).serve_forever()
