"""Actual isolated native stack, no legacy broker, no real platform calls."""
import os,sys,json,uuid,subprocess,time,signal,socket,shutil,re
from pathlib import Path
from urllib.request import Request,urlopen,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError,URLError
import psycopg

repo=Path(__file__).resolve().parents[2]
target=Path(os.environ.get("VIDEOROLL_TEST_SCHEMA_BINARY",str(repo/"rust/target/debug/videoroll-v2"))).resolve(strict=True).parent
url=os.environ["DATABASE_URL"]
assert "127.0.0.1:" in url and url.endswith("/videoroll_test")
root=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
root.mkdir(parents=True,exist_ok=True)
env={**os.environ,"VIDEOROLL_V2_STANDALONE":"true","VIDEOROLL_V2_NATIVE_OUTBOX_ONLY":"true",
    "VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH":"false","VIDEOROLL_V2_COMPAT_WRITE":"false",
    "VIDEOROLL_V2_COMPAT_URL":"","VIDEOROLL_V2_LEGACY_ORCHESTRATOR_URL":"","VIDEOROLL_V2_WORKER_GATEWAY":"",
    "INTERNAL_API_SECRET":"isolated-native-stack-secret-0123456789",
    "ADMIN_BOOTSTRAP_SECRET":"isolated-browser-bootstrap-not-production",
    "VIDEOROLL_V2_SOURCE_SCANNER":"false","VIDEOROLL_V2_REMOTE_RENDER":"false","VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI":"false",
    "VIDEOROLL_V2_ENABLE_SAU_PUBLISH":"false","VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK":"false",
    "VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN":"false","VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH":"false",
    "VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX":"false","VIDEOROLL_V2_WORKERS":"1"}
def db():return psycopg.connect(url)
def check(ok=True,startup=False,**overrides):
    r=subprocess.run([str(target/"videoroll-v2"),"standalone-preflight",*(["--startup"] if startup else [])],env={**env,**overrides},capture_output=True,text=True,timeout=30)
    assert (r.returncode==0)==ok,(r.stdout,r.stderr)
    if "{" in r.stdout:return json.loads(r.stdout[r.stdout.index("{"):])
assert check()["ready"] is True
check(False,VIDEOROLL_V2_COMPAT_URL="http://127.0.0.1:19999")
check(False,VIDEOROLL_V2_WORKER_GATEWAY="http://127.0.0.1:19999")
check(False,VIDEOROLL_V2_LEGACY_ORCHESTRATOR_URL="http://127.0.0.1:19999")
check(False,VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="false")
check(False,VIDEOROLL_V2_ENABLE_SAU_PUBLISH="true",VIDEOROLL_V2_SOCIAL_PUBLISHER_URL="http://127.0.0.1:18088")
t=uuid.uuid4()
with db() as c:
    c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES(%s,'local','own','created','python',0,0)",(t,))
assert check(False)["historical_blockers"]["legacy_tasks"]==1
with db() as c:
    c.execute("UPDATE tasks SET runtime_owner='rust' WHERE id=%s",(t,))
    e=uuid.uuid4()
    c.execute("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key) VALUES(%s,'custom','task',%s,'legacy.unknown','{}',%s)",(e,str(t),"fixture:"+str(e)))
assert check(False)["outbox"]["unresolved_delivery_rows"]==1
with db() as c:
    c.execute("DELETE FROM outbox_events WHERE id=%s",(e,))
    version,checksum=c.execute("SELECT version,checksum FROM _sqlx_migrations ORDER BY version LIMIT 1").fetchone()
    c.execute("UPDATE _sqlx_migrations SET checksum=%s WHERE version=%s",(b"wrong",version))
assert version in check(False)["invalid_migrations"]
with db() as c:
    c.execute("UPDATE _sqlx_migrations SET checksum=%s WHERE version=%s",(checksum,version))
    c.execute("INSERT INTO app_settings(key,value_json) VALUES('youtube.settings','{\"home_scan_enabled\":false}') ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json")
assert check()["ready"] is True
# Deliberately falsify ONLY the disposable database's recorded version.
# Verify both initial and startup gates are read-only and fail closed.
with db() as c:
    recorded,actual=c.execute("SELECT datcollversion,pg_database_collation_actual_version(oid) FROM pg_database WHERE datname=current_database()").fetchone()
    assert recorded==actual
    c.execute("UPDATE pg_database SET datcollversion='0.0-fixture' WHERE datname=current_database()")
for startup in (False,True):
    blocked=check(False,startup=startup)
    assert blocked["database_collation"]=={"recorded":"0.0-fixture","actual":actual,"compatible":False},blocked
with db() as c:
    assert c.execute("SELECT datcollversion FROM pg_database WHERE datname=current_database()").fetchone()[0]=="0.0-fixture"
# This is fixture metadata restoration, not production repair. Indexes were
# built with the same actual libc throughout the test.
with db() as c:
    c.execute("UPDATE pg_database SET datcollversion=%s WHERE datname=current_database()",(recorded,))
assert check()["database_collation"]["compatible"] is True
print("PASS: both standalone gates reject mismatched database collation; no implicit metadata repair",flush=True)
# A native ambiguous publication must block the initial cutover, but not
# prevent the entire control plane from recovering after a crash.
j=uuid.uuid4()
with db() as c:
    c.execute("INSERT INTO publish_jobs(id,task_id,platform,state,meta_json) VALUES(%s,%s,'bilibili','unknown','{}')",(j,t))
assert check(False)["historical_blockers"]["uncertain_publications"]==1
assert check(startup=True)["ready"] is True
with db() as c:
    assert c.execute("SELECT state FROM publish_jobs WHERE id=%s",(j,)).fetchone()[0]=="unknown"
    c.execute("DELETE FROM publish_jobs WHERE id=%s",(j,))
print("PASS: startup leaves native uncertain publication quarantined without blocking recovery",flush=True)
print("PASS: standalone admission rejects legacy fallback, old ownership, unresolved outbox and checksum mismatch",flush=True)

# Use dynamically allocated loopback ports, except production template's
# fixed SAU mapping is not used by this disabled-social fixture.
def port():
    with socket.socket() as s:s.bind(("127.0.0.1",0));return s.getsockname()[1]
ports={name:port() for name in ["api","bilibili","subtitle","youtube","egress","sau","web"]}
assert len(set(ports.values()))==len(ports)
procs=[]
logs=[]
def start(name,args,extra=None):
    log=root/(name+".log")
    f=log.open("w");logs.append(f)
    p=subprocess.Popen([str(a) for a in args],env={**env,**(extra or {})},stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    procs.append((p,log))
    return p
def get(p,path,token=None):
    headers={"X-Forwarded-Proto":"https"}
    if token:headers["x-videoroll-v2-internal-token"]=token
    try:r=urlopen(Request(f"http://127.0.0.1:{p}"+path,headers=headers),timeout=5)
    except HTTPError as e:r=e
    with r:return r.status,r.read(),dict(r.headers)
def wait(p,path):
    for _ in range(60):
        try:
            code,body,_=get(p,path)
            if code==200:return
        except (URLError,ConnectionError):pass
        for proc,log in procs:assert proc.poll() is None,log.read_text()
        time.sleep(.2)
    raise AssertionError(f"port {p} not ready")
try:
    start("api",[target/"videoroll-v2","api"],{"VIDEOROLL_V2_BIND":f"127.0.0.1:{ports['api']}"})
    wait(ports["api"],"/ready")
    for name,role in [("bilibili","bilibili-publisher"),("subtitle","subtitle-service"),("youtube","youtube-ingest")]:
        start(name,[target/"videoroll-v2","service",role],{"VIDEOROLL_V2_BIND":f"127.0.0.1:{ports[name]}"})
        wait(ports[name],"/health")
    start("egress",[target/"videoroll-egress"],{"VIDEOROLL_V2_EGRESS_BIND":f"127.0.0.1:{ports['egress']}"})
    wait(ports["egress"],"/health")
    start("sau",[target/"videoroll-sau"],{"VIDEOROLL_V2_SAU_BIND":f"127.0.0.1:{ports['sau']}"})
    wait(ports["sau"],"/health")
    start("worker",[target/"videoroll-v2","worker"])
    # Test the exact gateway template except isolated paths and ports.
    runtime=root/"nginx";runtime.mkdir()
    # Isolated fixture only: allow the unprivileged Nginx worker to traverse.
    root.chmod(0o755)
    web=root/"web"
    release=os.environ.get("VIDEOROLL_TEST_RELEASE_ROOT")
    if release:
        release=Path(release).resolve(strict=True)
        assert target==release/"bin"
        shutil.copytree(release/"web",web)
        config_source=release/"deploy/rust-native/nginx.conf"
        mime=release/"share/nginx/mime.types"
        nginx=str(release/"bin/nginx")
    else:
        if os.environ.get("VIDEOROLL_TEST_BROWSER")=="true":
            shutil.copytree(repo/"src/web/dist",web)
        else:
            web.mkdir();(web/"index.html").write_text("<html>isolated-native-web</html>")
        config_source=repo/"deploy/rust-native/nginx.conf"
        mime=Path("/opt/videoroll/share/nginx/mime.types")
        nginx="/opt/videoroll/bin/nginx"
    config=config_source.read_text().replace("worker_processes 2;","worker_processes 1;").replace("/opt/videoroll-rust/current/share/nginx/mime.types",str(mime)).replace("/run/videoroll-rust-nginx",str(runtime)).replace("/opt/videoroll-rust/current/web",str(web)).replace("127.0.0.1:18080",f"127.0.0.1:{ports['api']}").replace("127.0.0.1:13001",f"127.0.0.1:{ports['web']}")
    conf=runtime/"nginx.conf";conf.write_text(config)
    subprocess.run([nginx,"-t","-c",str(conf),"-p",str(runtime)],check=True,timeout=10)
    start("nginx",[nginx,"-c",conf,"-p",runtime,"-g","daemon off;"])
    wait(ports["web"],"/api/health")
    p=ports["web"]
    for path in ("/api/health","/api/v1/health","/api/auth/status","/api/v1/auth/status","/render-workers/v1/protocol"):
        code,body,_=get(p,path)
        assert code==200,(path,code,body)
        json.loads(body)
    assert get(p,"/api/v2/queue",env["VIDEOROLL_V2_INTERNAL_TOKEN"])[0]==404
    code,body,_=get(ports["api"],"/api/v2/queue",env["VIDEOROLL_V2_INTERNAL_TOKEN"])
    assert code==200,(code,body)
    assert get(p,"/api/desktop/authorize")[0]==404
    assert get(p,"/internal/desktop-auth")[0]==404
    assert get(p,"/social-login/vnc.html")[0] in (401,403)
    assert get(p,"/openapi.json")[0]==404
    index=(web/"index.html").read_bytes()
    assert get(p,"/workflow-ui/")[1]==index
    assert get(p,"/")[1]==index
    if release:
        paths=re.findall(rb'(?:src|href)="(/assets/[^"]+)"',index)
        assert any(path.endswith(b".js") for path in paths)
        for raw in paths:
            path=raw.decode("ascii")
            code,body,_=get(p,path)
            assert code==200 and body==(web/path.lstrip("/")).read_bytes(),path
        print(f"PASS: staged production index and {len(paths)} entry assets served byte-for-byte",flush=True)
    if os.environ.get("VIDEOROLL_TEST_BROWSER")=="true":
        from native_browser_isolated import browser
        browser(root,p,str(t),env["ADMIN_BOOTSTRAP_SECRET"])
    for proc,log in procs:assert proc.poll() is None,log.read_text()
    print("PASS: seven native processes and real Nginx; original browser routes, V1/V2 API, worker protocol, protected noVNC and workflow redirect; no broker",flush=True)
finally:
    for proc,log in reversed(procs):
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=5)
    for f in logs:f.close()
