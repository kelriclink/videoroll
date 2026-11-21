"""INTEGRATION-TEST-ONLY deterministic fake stage executor.

DO NOT DEPLOY: it does not perform ASR, rendering or external publication.
Used to validate the Rust coordinator's six-stage state machine in a disposable DB.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        prefix = "/v2/stages/"
        if not self.path.startswith(prefix):
            return self.reply(404, {"error": "unknown route"})
        if self.headers.get("x-videoroll-v2-internal-token") != os.environ.get("VIDEOROLL_V2_INTERNAL_TOKEN"):
            return self.reply(403, {"error": "wrong token"})
        length = int(self.headers.get("content-length", "0"))
        if not 2 <= length <= 128_000:
            return self.reply(400, {"error": "invalid body"})
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self.reply(400, {"error": "invalid json"})
        if not req.get("operation_key") or not req.get("task_id"):
            return self.reply(400, {"error": "not idempotently addressable"})
        stage = self.path.removeprefix(prefix)
        if stage not in {"subtitle", "render", "dubbing", "publish"}:
            return self.reply(400, {"error": "unknown stage"})
        result = (
            {"confirmed": True, "external_id": "BVFAKE0000001"}
            if stage == "publish"
            else {"mock": True, "completed_stage": stage}
        )
        return self.reply(200, {"status": "completed", "result": result})

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18081), Handler).serve_forever()
