#!/usr/bin/env python3
"""YouDub V1 PUT control, file lock, real mock Unix resident, no GPU usage."""
import fcntl
import json
import os
import time
import uuid
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import psycopg

base = "http://127.0.0.1:18080/api/v1"
token = os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
db_url = os.environ["DATABASE_URL"]
runtime = Path(os.environ["VIDEOROLL_V2_YOUDUB_RUNTIME"])
root = Path(__file__).resolve().parents[1] / "target"
gpu_free = root / "mock-youdub-control-tools/free-mb.txt"
assert runtime.name == "mock-youdub-v2" and runtime.is_relative_to(root), runtime
assert gpu_free.exists() and "127.0.0.1:" in db_url and db_url.endswith("/videoroll_test")

def api(method, path, data=None, authenticated=True):
    hd = {"x-videoroll-v2-internal-token":token} if authenticated else {}
    if data is not None:
        hd["Content-Type"]="application/json"
    req = Request(base+path,method=method,headers=hd,
                  data=json.dumps(data).encode() if data is not None else None)
    try:
        response=urlopen(req,timeout=15)
    except HTTPError as err:
        response=err
    with response:
        raw=response.read()
        try: body=json.loads(raw)
        except ValueError:body=raw.decode(errors="replace")
        return response.status,body

def configure(enabled, resident_marker=None, include=True):
    obj={"enabled":enabled}
    if include:obj["resident"]=resident_marker
    return api("PUT","/settings/dubbing/native",obj)

control=runtime/"videoroll-control.json"
pid_file=runtime/"voxcpm-resident.pid"
sock=runtime/"voxcpm-resident.sock"
before=control.read_bytes() if control.is_file() else None
runtime.mkdir(parents=True,exist_ok=True)
model=runtime/"models/OpenBMB__VoxCPM2-modelscope"
fake=[
    runtime/"backend/app/pipeline.py",
    runtime/"submodule/demucs/demucs/api.py",
    model/"model.safetensors",
    model/"audiovae.pth",
]
for file in fake:
    if file.exists():raise AssertionError("fake model fixture path already exists: "+str(file))
job_id=uuid.uuid4()
task_id=uuid.uuid4()
pid=None
try:
    assert api("PUT","/settings/dubbing/native",{"enabled":False},False)[0]==401
    assert api("GET","/settings/dubbing/native",authenticated=False)[0]==401
    assert configure(True,False)[0]==409,"uninstalled model must reject enabling"
    assert not control.exists() if before is None else control.read_bytes()==before
    code,disabled=configure(False,False)
    assert code==200 and not disabled["enabled"] and not disabled["resident"],(code,disabled)
    for file in fake:
        file.parent.mkdir(parents=True,exist_ok=True)
        if file.name=="model.safetensors":
            with file.open("wb") as fp:fp.truncate(1_100_000_000)
        else:file.write_bytes(b"mock model fixture")
    code,on_demand=configure(True,False)
    assert code==200 and on_demand["enabled"] and on_demand["installed"] and not on_demand["resident"],(code,on_demand)
    assert on_demand["accepting_jobs"] and on_demand["gpu_loaded"] is False
    assert json.loads(control.read_text())=={"enabled":True,"resident":False}
    # Original and Rust record readers both see the same atomic JSON file.
    assert api("GET","/settings/dubbing/native")[1]["mode"]=="on_demand"
    print("PASS: model availability and original on-demand control JSON, with authentication and no pretend GPU")

    # Exact fcntl flock path serializes with the Python maintenance GUI.
    lock=runtime/".videoroll-control.lock"
    outcome=[]
    with lock.open("a+b") as fp:
        fcntl.flock(fp,fcntl.LOCK_EX)
        thread=threading.Thread(target=lambda:outcome.append(configure(True,False)),daemon=True)
        thread.start()
        time.sleep(0.25)
        assert not outcome,"Rust PUT ignored original Python flock"
        fcntl.flock(fp,fcntl.LOCK_UN)
    thread.join(timeout=12)
    assert outcome and outcome[0][0]==200,outcome
    print("PASS: Rust configure waits for original Python fcntl lock and writes atomically")

    # Two original-table active job states; no shutdown or residency mode
    # change can strand an already queued VoiceDub task.
    with psycopg.connect(db_url) as db:
        db.execute("INSERT INTO tasks(id,source_type,source_license,status) VALUES(%s,'local','own','created')",(task_id,))
        db.execute("""INSERT INTO dubbing_jobs(id,task_id,status,request_json)
            VALUES(%s,%s,'queued','{"model_path":"youdub:voxcpm2"}'::jsonb)""",(job_id,task_id))
    assert configure(False,False)[0]==409
    assert configure(True,True)[0]==409
    assert configure(True,False)[0]==200
    assert json.loads(control.read_text())=={"enabled":True,"resident":False}
    with psycopg.connect(db_url) as db:
        db.execute("UPDATE dubbing_jobs SET status='failed' WHERE id=%s",(job_id,))
    print("PASS: active durable YouDub job blocks disable and resident-mode change")

    gpu_free.write_text("11000\n")
    code,low=configure(True,True)
    assert code==409,(code,low)
    assert not pid_file.exists()
    assert json.loads(control.read_text())=={"enabled":True,"resident":False}
    gpu_free.write_text("16000\n")
    code,started=configure(True,True)
    assert code==200 and started["enabled"] and started["resident"] and started["mode"]=="resident",(code,started)
    pid=int(pid_file.read_text())
    assert pid>2
    assert json.loads(control.read_text())=={"enabled":True,"resident":True}
    for _ in range(60):
        code,running=api("GET","/settings/dubbing/native")
        if code==200 and running["gpu_loaded"] is True:break
        time.sleep(0.1)
    else:raise AssertionError(("resident socket never ready",running))
    assert sock.is_socket()
    # Omitting resident must preserve Python's old resident flag.
    code,again=configure(True,include=False)
    assert code==200 and again["resident"] and int(pid_file.read_text())==pid,(code,again)
    print("PASS: 12000 MiB VRAM threshold; actual mock resident process starts, pings, and is reused")

    # PID file manipulation must never permit a different process to be killed.
    try:
        pid_file.write_text(str(os.getpid())+"\n")
        assert configure(False,False)[0]==409
        assert json.loads(control.read_text())=={"enabled":True,"resident":True}
    finally:
        pid_file.write_text(str(pid)+"\n")
    with psycopg.connect(db_url) as db:
        db.execute("UPDATE dubbing_jobs SET status='queued' WHERE id=%s",(job_id,))
    assert configure(False,False)[0]==409
    assert configure(True,False)[0]==409
    assert configure(True,include=False)[0]==200
    with psycopg.connect(db_url) as db:
        db.execute("UPDATE dubbing_jobs SET status='failed' WHERE id=%s",(job_id,))
    code,stopped=configure(False,False)
    assert code==200 and not stopped["enabled"] and not stopped["resident"],(code,stopped)
    assert not pid_file.exists() and not sock.exists()
    assert json.loads(control.read_text())=={"enabled":False,"resident":False}
    print("PASS: foreign PID protection, no busy shutdown, matching resident stopped and files cleared")

    code,disabled=configure(False,include=False)
    assert code==200 and not disabled["enabled"]
    with psycopg.connect(db_url) as db:
        db.execute("DELETE FROM dubbing_jobs WHERE id=%s",(job_id,))
        db.execute("DELETE FROM tasks WHERE id=%s",(task_id,))
    print("PASS: idempotent disabled state and original DB fixtures cleaned")
finally:
    if pid_file.is_file() and pid is not None:
        pid_file.write_text(str(pid)+"\n")
        try:configure(False,False)
        except Exception:pass
    with psycopg.connect(db_url) as db:
        db.execute("DELETE FROM dubbing_jobs WHERE id=%s",(job_id,))
        db.execute("DELETE FROM tasks WHERE id=%s",(task_id,))
    for file in fake:
        file.unlink(missing_ok=True)
    for path in [model,runtime/"models",runtime/"backend/app",runtime/"backend",
                 runtime/"submodule/demucs/demucs",runtime/"submodule/demucs",runtime/"submodule"]:
        try:path.rmdir()
        except OSError:pass
    if before is None:control.unlink(missing_ok=True)
    else:control.write_bytes(before)
