"""Local-only Bilibili native subtitle draft protocol simulator.

AID 101 returns success for CID 201.
AID 102 accepts the draft but reports HTTP 503 for CID 202; its response is
intentionally ambiguous, so the Rust worker must never retry preSave without
manual evidence. Only disposable tests can access this server.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

COUNTS = {"view": 0, "upload": 0, "presave_101": 0, "presave_102": 0, "presave_103_203": 0, "presave_103_204": 0}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        cookie=self.headers.get("Cookie","")
        return "SESSDATA=video-roll-test" in cookie

    def do_GET(self):
        u=urlparse(self.path)
        if u.path=="/__test_state":
            return self.reply(200,COUNTS)
        if not self.authorized():
            return self.reply(403,{"code":-101,"message":"missing test cookie"})
        if u.path=="/x/web-interface/view":
            COUNTS["view"]+=1
            aid=parse_qs(u.query).get("aid",[""])[0]
            if aid in ("101","102","103"):
                cids={"101":[201],"102":[202],"103":[203,204]}[aid]
                return self.reply(200,{"code":0,"data":{"pages":[
                    {"cid":cid} for cid in cids
                ]}})
        return self.reply(404,{"code":-404})

    def do_POST(self):
        u=urlparse(self.path)
        if not self.authorized():
            return self.reply(403,{"code":-101})
        length=int(self.headers.get("Content-Length","0"))
        if length<2 or length>16*1024*1024:
            return self.reply(400,{"code":-403})
        raw=self.rfile.read(length)
        if u.path=="/x/upload/web/image":
            if (b"bucket" not in raw or b"subtitle" not in raw
                    or b"application/x-subrip" not in raw
                    or b"Hello from a Rust integration test" not in raw):
                return self.reply(400,{"code":-403,"message":"wrong SRT multipart payload"})
            COUNTS["upload"]+=1
            return self.reply(200,{"code":0,"data":{"location":"https://mock.invalid/subtitle.srt"}})
        if u.path=="/x/v2/dm/subtitle/draft/preSave":
            params=parse_qs(raw.decode())
            if params.get("type")!=["1"] or params.get("csrf")!=["1234567890123456"]:
                return self.reply(400,{"code":-403,"message":"wrong draft form"})
            try:
                fields=json.loads(params["files"][0])
                assert len(fields)==1 and fields[0]["lan"]=="zh" and fields[0]["subtitle_id"]==0
                assert fields[0]["url"]=="https://mock.invalid/subtitle.srt"
            except (KeyError,ValueError,IndexError,AssertionError):
                return self.reply(400,{"code":-403,"message":"wrong subtitle files"})
            aid=params.get("aid",[""])[0]
            cid=params.get("oid",[""])[0]
            if aid=="101" and cid=="201":
                COUNTS["presave_101"]+=1
                return self.reply(200,{"code":0,"data":{"submitted":True}})
            if aid=="102" and cid=="202":
                COUNTS["presave_102"]+=1
                return self.reply(503,{"code":-503,"message":"simulated uncertain upstream"})
            if aid=="103" and cid in ("203","204"):
                COUNTS[f"presave_103_{cid}"]+=1
                return self.reply(200,{"code":0,"data":{"submitted":True}})
        return self.reply(404,{"code":-404})

    def log_message(self,*args):
        pass


if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",18084),Handler).serve_forever()
