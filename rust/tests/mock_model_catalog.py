"""Disposable local OpenAI /models backend: deterministic, token-aware, no secrets logged."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!="/v1/models":
            return self.respond(404,{"detail":"not found"})
        if self.headers.get("Authorization")!="Bearer model-test-key":
            return self.respond(403,{"detail":"unauthorized"})
        return self.respond(200,{"data":[
            {"id":"model-z","top_provider":{
                "context_length":131072,"max_completion_tokens":8192}},
            {"id":"model-b","context_window":"32768",
                "max_output_tokens":"6400"},
            {"id":"model-a","context_length":1000,
                "max_completion_tokens":1000},
            {"id":"model-z","context_window":8000},
            {"id":"model-without-limits"},
            {"id":"invalid-cap","max_model_len":-2},
            {"id":False},{"id":""},
        ]})
    def respond(self,status,obj):
        data=json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type","application/json")
        self.send_header("content-length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self,*_):
        pass

if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",18088),Handler).serve_forever()
