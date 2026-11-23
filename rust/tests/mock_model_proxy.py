"""Tiny *isolated* forward HTTP proxy, no internet access and no real HF account.
Handles Python httpx and Rust reqwest when testing /subtitle/models/proxy/test.
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=="http://huggingface.co/robots.txt":
            return self.respond(200,b"User-agent: *\nAllow: /\n")
        if self.path=="http://huggingface.co/blocked":
            return self.respond(403,b"access denied")
        return self.respond(404,b"not a model proxy fixture")
    def respond(self,status,body):
        self.send_response(status)
        self.send_header("Content-Type","text/plain")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*a):pass
ThreadingHTTPServer(("127.0.0.1",18089),Handler).serve_forever()
