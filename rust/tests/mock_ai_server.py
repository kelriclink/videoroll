"""Local mock ASR server used only by isolated integration tests."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/v1/chat/completions":
            return self.chat()
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
            "segments":[
                {"id":0,"start":0.2,"end":0.6,"text":"Hello from a Rust integration test."},
                {"id":1,"start":0.7,"end":1.2,"text":"A second subtitle."},
                {"id":2,"start":1.3,"end":1.7,"text":"A third subtitle."}
            ],
        })
    def chat(self):
        length=int(self.headers.get("content-length","0"))
        if not 10 < length < 300_000:
            return self.reply(413,{"error":"model request too large"})
        try:
            body=json.loads(self.rfile.read(length))
            model=body.get("model")
            if model=="rag-test-model":
                messages=body["messages"]
                result=(
                    {"action":"finish","summary":"An electronics video.",
                     "terms":[{"source":"Rust","target":"Rust","note":"proper noun"}]}
                    if len(messages)>=4 else
                    {"action":"search_knowledge","query":"Rust integration"}
                )
            elif model=="Index-Translate-35B-A3B":
                user=body["messages"][-1]["content"]
                lines=json.loads(user.split("\n")[-1])
                if not lines or any("idx" not in row or "text" not in row for row in lines):
                    return self.reply(400,{"error":"wrong subtitle format"})
                rows=lines[:-1] if len(lines)>1 else lines
                result={"translations":[
                    {"idx":row["idx"],"text":"译文："+row["text"]}
                    for row in rows
                ]}
            else:
                return self.reply(400,{"error":"unknown test model"})
            return self.reply(200,{"choices":[{"message":{
                "content":json.dumps(result,ensure_ascii=False)
            }}]})
        except (KeyError,ValueError,TypeError,IndexError) as error:
            return self.reply(400,{"error":str(error)})
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
