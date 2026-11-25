"""Test-only framed bridge. Never touches models, network or database."""
import json
import os
import struct
import sys
import time

assert sys.argv[1:] == ["--serve"]
n = 0
while True:
    header = sys.stdin.buffer.read(4)
    if not header:
        break
    length = struct.unpack(">I", header)[0]
    req = json.loads(sys.stdin.buffer.read(length))
    n += 1
    op = req.get("op")
    if op == "hang":
        time.sleep(60)
    elif op == "crash":
        os._exit(42)
    elif op == "oversize":
        sys.stdout.buffer.write(struct.pack(">I", 13 * 1024 * 1024))
        sys.stdout.buffer.flush()
        time.sleep(60)
    elif op == "badjson":
        sys.stdout.buffer.write(struct.pack(">I", 1) + b"{")
        sys.stdout.buffer.flush()
        time.sleep(60)
    elif op == "error":
        out = {"ok": False, "error_type": "RuntimeError"}
    else:
        out = {"ok": True, "result": {"pid": os.getpid(), "calls": n, "value": req.get("value")}}
    data = json.dumps(out).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(data)) + data)
    sys.stdout.buffer.flush()
