"""Real ffplayout + exact native Nginx routing, no production DB/media/stream."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

repo = Path(__file__).resolve().parents[2]
binary = Path(os.environ.get("VIDEOROLL_TEST_FFPLAYOUT", "/opt/videoroll/bin/ffplayout")).resolve(strict=True)
nginx = Path(os.environ.get("VIDEOROLL_TEST_NGINX", "/opt/videoroll/bin/nginx")).resolve(strict=True)
mime = Path(os.environ.get("VIDEOROLL_TEST_MIME", "/opt/videoroll/share/nginx/mime.types")).resolve(strict=True)

def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

with tempfile.TemporaryDirectory(prefix="vr2-ffplayout-") as directory:
    root = Path(directory)
    root.chmod(0o755)
    for name in ("db", "storage", "logs", "public", "playlists", "nginx", "web", "home"):
        (root/name).mkdir()
    (root/"web/index.html").write_text("isolated VideoRoll gateway")
    env = {**os.environ, "HOME": str(root/"home"), "TZ": "UTC"}
    # Explicit CLI paths, not the installed instance's database or environment.
    args = [str(binary), "--db", str(root/"db/isolated.db")]
    password = "isolated-playout-not-production-123"
    init = subprocess.run(args+["--init","--username","fixture","--password",password,"--mail","fixture@example.invalid",
        "--two-factor","false","--storage",str(root/"storage"),"--logs",str(root/"logs"),
        "--public",str(root/"public"),"--playlists",str(root/"playlists"),
        "--smtp-server","","--smtp-user","","--smtp-password","","--smtp-starttls","false","--smtp-port","25"],
        env=env,cwd=root,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30)
    assert init.returncode == 0 and (root/"db/isolated.db").is_file(), (init.returncode,init.stderr[-2000:])
    media = root/"storage"
    # The upstream may use a per-channel subdirectory; fixture sources are
    # absolute paths contained in the dedicated test tree.
    for name,color in (("first.mp4","red"),("second.mp4","blue")):
        subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i",f"color={color}:s=320x180:r=25:d=12",
            "-f","lavfi","-i","anullsrc=r=48000:cl=stereo","-t","12","-c:v","libx264","-pix_fmt","yuv420p",
            "-c:a","aac","-y",str(media/name)],check=True,timeout=30)
    playout_port,web_port,dead_backend=port(),port(),port()
    assert len({playout_port,web_port,dead_backend})==3
    config=(repo/"deploy/rust-native/nginx.conf").read_text().replace(
        "worker_processes 2;","worker_processes 1;").replace(
        "/opt/videoroll-rust/current/share/nginx/mime.types",str(mime)).replace(
        "/run/videoroll-rust-nginx",str(root/"nginx")).replace(
        "/opt/videoroll-rust/current/web",str(root/"web")).replace(
        "127.0.0.1:18080",f"127.0.0.1:{dead_backend}").replace(
        "127.0.0.1:13001",f"127.0.0.1:{web_port}").replace(
        "127.0.0.1:8787",f"127.0.0.1:{playout_port}")
    conf=root/"nginx/nginx.conf"
    conf.write_text(config)
    subprocess.run([str(nginx),"-t","-c",str(conf),"-p",str(root/"nginx")],check=True,timeout=10)
    processes=[]
    logs=[]
    def start(name,argv):
        log=root/(name+".log")
        file=log.open("w")
        logs.append(file)
        p=subprocess.Popen(argv,env=env,cwd=root,stdin=subprocess.DEVNULL,stdout=file,stderr=subprocess.STDOUT,start_new_session=True)
        processes.append((p,log))
    def call(path,body=None,token=None,method=None):
        headers={"Content-Type":"application/json"}
        if token:headers["Authorization"]="Bearer "+token
        req=Request(f"http://127.0.0.1:{web_port}"+path,
            data=json.dumps(body).encode() if body is not None else None,headers=headers,
            method=method or ("POST" if body is not None else "GET"))
        try:response=urlopen(req,timeout=15)
        except HTTPError as e:response=e
        with response:
            data=response.read()
            try:data=json.loads(data)
            except ValueError:pass
            return response.status,data
    try:
        # Process-local null output: encode and discard, never RTMP/SRT/HLS.
        start("ffplayout",args+["--listen",f"127.0.0.1:{playout_port}","--output","null","--start","now","--log-to-console"])
        start("nginx",[str(nginx),"-c",str(conf),"-p",str(root/"nginx"),"-g","daemon off;"])
        for _ in range(100):
            try:
                if call("/api/setup")[0]==200:break
            except (URLError,ConnectionError):pass
            for p,log in processes:assert p.poll() is None,log.read_text()[-3000:]
            time.sleep(.2)
        else:raise AssertionError("isolated gateway unavailable")
        code,setup=call("/api/setup")
        assert code==200 and setup["required"] is False,(code,setup)
        assert call("/api/channels")[0] in (401,403)
        code,auth=call("/auth/login",{"username":"fixture","password":password})
        assert code==200 and isinstance(auth["access"],str)
        token=auth["access"]
        def ok(path,body=None,method=None):
            code,result=call(path,body,token,method)
            assert code==200,(path,code,result)
            return result
        channels=ok("/api/channels")
        assert any(c["id"]==1 for c in channels),channels
        date=dt.datetime.now(dt.timezone.utc).date().isoformat()
        def clip(name):return {"source":str(media/name),"in":0.0,"out":12.0,"duration":12.0}
        playlist={"channel":"Channel 1","date":date,"program":[clip("first.mp4"),clip("second.mp4")]}
        ok("/api/playlist/1",playlist)
        result=ok("/api/playlist/1?date="+date)
        assert [Path(c["source"]).name for c in result["program"]]==["first.mp4","second.mp4"]
        playlist["program"].reverse()
        playlist["program"][0]["in"]=1.0
        ok("/api/playlist/1",playlist)
        result=ok("/api/playlist/1?date="+date)
        assert [Path(c["source"]).name for c in result["program"]]==["second.mp4","first.mp4"]
        assert result["program"][0]["in"]==1.0
        print("PASS: real ffplayout auth, save/read playlist, reorder and trim through native Nginx",flush=True)
        control="/api/control/1/process"
        ok(control,{"command":"stop"})
        assert ok(control,{"command":"status"})=="not running"
        ok(control,{"command":"start"})
        for _ in range(30):
            if ok(control,{"command":"status"})=="active":break
            time.sleep(.2)
        else:raise AssertionError("channel did not start")
        for _ in range(40):
            current=ok("/api/control/1/media/current")
            if Path(current.get("media",{}).get("source","")).name in ("first.mp4","second.mp4"):break
            time.sleep(.25)
        else:raise AssertionError(("no actual fixture clip selected",current))
        ok(control,{"command":"stop"})
        for _ in range(30):
            if ok(control,{"command":"status"})=="not running":break
            time.sleep(.2)
        else:raise AssertionError("channel did not stop")
        print("PASS: real ffplayout start/status/current media query/stop, null output",flush=True)
        ok("/api/playlist/1/"+date,method="DELETE")
        code,_=call("/api/playlist/1?date="+date,token=token)
        assert code==204,code
        print("PASS: real playlist deletion via gateway; production data untouched",flush=True)
    except Exception:
        for _,log in processes:
            print(log.name,log.read_text(errors="replace")[-4000:])
        raise
    finally:
        for p,_ in reversed(processes):
            if p.poll() is None:
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=5)
        for f in logs:f.close()
