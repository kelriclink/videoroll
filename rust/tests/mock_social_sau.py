"""Disposable, auth-enforcing SAU browser-sidecar test double. Never logs storage_state."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import hashlib, hmac, json, os, uuid

SECRET = os.environ["INTERNAL_API_SECRET"]
TOKEN = "v1." + hmac.new(SECRET.encode(), b"videoroll-internal-service:v1", hashlib.sha256).hexdigest()
PLATFORMS = {"douyin", "xiaohongshu", "kuaishou"}
SESSION = str(uuid.uuid4())

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def output(self, data, status=200):
        raw=json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def operation(self):
        if not hmac.compare_digest(self.headers.get("x-videoroll-internal-token", ""), TOKEN):
            return self.output({"detail":"authentication rejected"},403)
        path=urlparse(self.path).path
        params=parse_qs(urlparse(self.path).query)
        method=self.command
        if path=="/accounts" and method=="GET":
            if "platform" in params and params["platform"][0] not in PLATFORMS:
                return self.output({"detail":"invalid platform"},400)
            return self.output([{"id":"1e2baeba-d95b-44dd-bf7b-aedb0ce5cd7a","platform":"douyin","name":"mock-account","is_active":True,"check_state":"valid"}])
        if path.startswith("/login-sessions/") and method=="POST":
            platform=path.split("/")[-1]
            if platform not in PLATFORMS:
                return self.output({"detail":"unsupported platform"},400)
            length=int(self.headers.get("Content-Length","0"))
            body=json.loads(self.rfile.read(length))
            return self.output({"id":SESSION,"platform":platform,"account_name":body["account_name"],"state":"pending","message":None,"browser_url":"http://127.0.0.1:8090/session","created_at":"2026-11-22T00:00:00Z","finished_at":None})
        if path.startswith("/login-sessions/") and method in ("GET","DELETE"):
            if path.split("/")[-1]!=SESSION:
                return self.output({"detail":"not found"},404)
            return self.output({"id":SESSION,"platform":"douyin","account_name":"Test","state":"canceled" if method=="DELETE" else "pending","message":None,"browser_url":"http://127.0.0.1:8090/session","created_at":"2026-11-22T00:00:00Z","finished_at":None})
        if path.startswith("/accounts/") and method=="POST":
            item=path.split("/")[-1]
            if item=="check" and path.split("/")[-2]=="1e2baeba-d95b-44dd-bf7b-aedb0ce5cd7a":
                account_id=path.split("/")[-2]
                return self.output({"account":{"id":account_id,"platform":"douyin","name":"mock-account","check_state":"valid"},"check_job_id":"mock"})
            if item in PLATFORMS:
                content_len=int(self.headers.get("Content-Length","0"))
                if content_len>2*1024*1024:
                    return self.output({"detail":"file too large"},413)
                raw=self.rfile.read(content_len)
                if b"storage_state.json" not in raw:
                    return self.output({"detail":"file not received"},400)
                return self.output({"account":{"id":str(uuid.uuid4()),"platform":item,"name":"imported","check_state":"pending"},"check_job_id":"mock"})
        if path.startswith("/accounts/") and method=="DELETE":
            return self.output({"ok":True})
        return self.output({"detail":"unexpected mock SAU request"},404)

    do_GET=operation
    do_POST=operation
    do_DELETE=operation

if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",18085),Handler).serve_forever()
