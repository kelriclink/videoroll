"""Isolated fallback backend. Never uses original production services."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/compat/read":
            return self.reply(200, {"source": "legacy-mock", "read_only": True})
        return self.reply(404, {"detail": "not found"})

    def do_POST(self):
        return self.reply(200, {"source": "legacy-mock", "side_effect": True})

    def reply(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18083), Handler).serve_forever()
