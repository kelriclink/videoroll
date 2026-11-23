"""Disposable real HTTP endpoint for External Whisper, Groq and CF test paths.
Checks upload type/model/authorization and returns timed segments. No Internet.
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,base64
class Handler(BaseHTTPRequestHandler):
 def do_POST(self):
  size=int(self.headers.get("Content-Length") or "0")
  body=self.rfile.read(size)
  auth=self.headers.get("Authorization") or ""
  if self.path.endswith("/audio/transcriptions"):
   is_groq="/groq/" in self.path
   must=b"whisper-large-v3-turbo" if is_groq else b"whisper-test"
   key="groq-fixture" if is_groq else "external-fixture"
   if not is_groq and (b'name="batch_size"\r\n\r\n3' not in body or b'name="vad_filter"\r\n\r\nfalse' not in body or b'name="speech_pad_ms"\r\n\r\n0' not in body):
    return self.answer(400,{"error":"External Whisper options not forwarded"})
   if auth!="Bearer "+key or must not in body or (is_groq and b"fLaC" not in body) or (not is_groq and b"RIFF" not in body):
    return self.answer(400,{"error":"bad ASR multipart or credential"})
   text="Groq test success" if is_groq else "External test success"
   return self.answer(200,{"duration":1.0,"segments":[{"start":0.1,"end":0.9,"text":text}]})
  if self.path=="/client/v4/accounts/testacct/ai/run/@cf/openai/whisper-large-v3-turbo":
   if auth!="Bearer cf-fixture":
    return self.answer(403,{"success":False,"errors":["bad token"]})
   try:
    d=json.loads(body)
    raw=base64.b64decode(d["audio"],validate=True)
    if raw[:4]!=b"RIFF" or d["task"]!="transcribe":raise ValueError("invalid WAV")
   except Exception:
    return self.answer(400,{"success":False,"errors":["invalid WAV"]})
   return self.answer(200,{"success":True,"result":{
       "segments":[{"start":0.2,"end":0.8,"text":"Cloudflare test success"}]}})
  return self.answer(404,{"error":"wrong provider endpoint"})
 def answer(self,status,value):
  raw=json.dumps(value).encode()
  self.send_response(status);self.send_header("content-type","application/json")
  self.send_header("content-length",str(len(raw)));self.end_headers();self.wfile.write(raw)
 def log_message(self,*_):pass
ThreadingHTTPServer(("127.0.0.1",18091),Handler).serve_forever()
