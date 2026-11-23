"""Isolated deterministic Hugging Face Hub model API fixture.

Exposes the actual /api/models/.../revision/... and /resolve/... URL shapes
used by the Rust native snapshot downloader. No Internet or credentials.
"""
import json
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import unquote,urlsplit

REPO="Systran/faster-whisper-tiny"
FILES={
 "main":{
  "config.json":b'{"model":"tiny","revision":"main"}',
  "weights/model.bin":bytes(range(256))*4,
 },
 "v2":{
  "config.json":b'{"model":"tiny","revision":"v2"}',
  "weights/model.bin":b"model-v2-weights-updated",
 },
}

EMBEDDING_REPO="BAAI/bge-small-zh-v1.5"
EMBEDDING_FILES={"main":{
  "config.json":b'{"model_type":"bert","hidden_size":3}',
  "weights/embedding.bin":b"offline-embedding-fixture",
}}

class Handler(BaseHTTPRequestHandler):
 def reply(self,status,data,type_="application/octet-stream"):
  self.send_response(status)
  self.send_header("Content-Type",type_)
  self.send_header("Content-Length",str(len(data)))
  self.end_headers()
  self.wfile.write(data)
 def do_GET(self):
  path=unquote(urlsplit(self.path).path)
  for repo,revisions in ((REPO,FILES),(EMBEDDING_REPO,EMBEDDING_FILES)):
   for rev,files in revisions.items():
    if path==f"/api/models/{repo}/revision/{rev}":
     data=json.dumps({"siblings":[{"rfilename":key,"size":len(blob)}
              for key,blob in files.items()]}).encode()
     return self.reply(200,data,"application/json")
    start=f"/{repo}/resolve/{rev}/"
    if path.startswith(start):
     key=path[len(start):]
     if key in files:
      return self.reply(200,files[key])
  return self.reply(404,b"not found")
 def log_message(self,*_):pass

ThreadingHTTPServer(("127.0.0.1",18090),Handler).serve_forever()
