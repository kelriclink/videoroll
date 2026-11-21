"""Local mock ASR server used only by isolated integration tests."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/audio/transcriptions":
            return self.reply(404, {"error":"unknown model route"})
        length=int(self.headers.get("content-length","0"))
        if length <= 100 or length>32*1024*1024:
            return self.reply(400, {"error":"missing or excessively large FLAC body"})
        body=self.rfile.read(length)
        if b"whisper-mock" not in body or b"audio.flac" not in body:
            return self.reply(400, {"error":"multipart ASR form absent"})
        return self.reply(200, {
            "text":"Hello from a Rust integration test.",
            "segments":[{"id":0,"start":0.2,"end":1.5,"text":"Hello from a Rust integration test."}],
        })
    def reply(self,status,payload):
        data=json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self,*args):
        pass

if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",18082),Handler).serve_forever()
