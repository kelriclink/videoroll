#!/usr/bin/env python3
"""Root-only local logical backup and isolated restore. No application starts.
Source commands are read-only. Never print backup data or database logs.
"""
import argparse, hashlib, json, os, re, secrets, shutil, subprocess, tempfile, time
from pathlib import Path
from urllib.parse import quote

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source",default="postgres",choices=["postgres"])
parser.add_argument("--backup-parent",default="/4t")
parser.add_argument("--binary",required=True,type=Path)
parser.add_argument("--reuse-backup",type=Path,help="reuse a previously completed, checksummed private local archive")
parser.add_argument("--restore-jobs",type=int,default=4,choices=range(1,9))
parser.add_argument("--restore-timeout",type=int,default=1800)
parser.add_argument("--confirm-local-backup",action="store_true",required=True)
a=parser.parse_args()
assert 300<=a.restore_timeout<=7200
assert os.geteuid()==0,"root required for private backup"
os.umask(0o077)
parent=Path(a.backup_parent).resolve(strict=True)
assert parent.is_dir() and parent!=Path("/")
binary=a.binary.resolve(strict=True)
assert binary.is_file()
if a.reuse_backup:
    root=a.reuse_backup.resolve(strict=True)
    assert root.parent==parent and root.name.startswith("vr-db-rehearsal.")
    assert root.stat().st_uid==0 and root.stat().st_mode&0o077==0
    assert not (root/"socket").exists(),"remove only the verified stopped clone socket directory before reuse"
else:
    root=Path(tempfile.mkdtemp(prefix="vr-db-rehearsal.",dir=parent))
archive=root/"videoroll.dump"
report={"backup_directory":str(root)}
name="vr-db-rehearsal-"+secrets.token_hex(8)
volume=name+"-data"
sock=root/"socket";sock.mkdir(mode=0o700);sock.chmod(0o777)
storage=root/"empty-storage";storage.mkdir(exist_ok=True)
assert not any(storage.iterdir())
log=root/"private.log"
print("private backup directory:",root,flush=True)

def proc(args,**kw):
    return subprocess.run(args,check=True,**kw)
def capture(args):
    with log.open("ab") as err:
        return proc(args,stdout=subprocess.PIPE,stderr=err,text=True).stdout.strip()
def source(sql):
    with log.open("ab") as err:
        return proc(["docker","exec","-i",a.source,"sh","-c",
            'PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=60000" psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d videoroll'],
            input=sql,text=True,stdout=subprocess.PIPE,stderr=err).stdout.strip()
def clone(sql,db="videoroll_test"):
    assert db in ("videoroll_test","postgres")
    with log.open("ab") as err:
        return proc(["docker","exec","-i",name,"psql","-X","-qAt","-v","ON_ERROR_STOP=1","-U","postgres","-d",db],
            input=sql,text=True,stdout=subprocess.PIPE,stderr=err).stdout.strip()
# Count all public physical tables using an exported source snapshot. Only
# table names/counts leave the SQL client; no row contents or credentials.
inventory=r"""
SELECT format('SELECT %L || '':'' || count(*)::text FROM %I.%I;',schemaname||'.'||tablename,schemaname,tablename)
FROM pg_tables WHERE schemaname='public' ORDER BY tablename
\gexec
"""
metadata="""SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND (NOT i.indisvalid OR NOT i.indisready);
SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND NOT c.convalidated;
SELECT count(*) FROM pg_indexes WHERE schemaname='public';
SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public';
"""
def counts(raw):
    pairs=[line.rsplit(":",1) for line in raw.splitlines()]
    assert all(re.fullmatch(r"public\.[a-zA-Z0-9_]+",k) and v.isdigit() for k,v in pairs)
    return {k:int(v) for k,v in pairs}
def sha(path):
    with path.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()
def mark(label):
    print("PASS:",label,flush=True)

# Restrict source to the known VideoRoll DB and exact local image ID.
image=capture(["docker","inspect",a.source,"--format","{{.Image}}"])
assert re.fullmatch(r"sha256:[0-9a-f]{64}",image)
report["source_image_id"]=image
source_info=source("""SELECT current_setting('server_version_num'),pg_database_size(current_database()),datlocprovider,datcollate,datctype,datcollversion,pg_database_collation_actual_version(oid)
FROM pg_database WHERE datname=current_database();""").split("|")
assert source_info[2:5]==["c","en_US.utf8","en_US.utf8"],"unexpected production locale; review manually"
assert all(re.fullmatch(r"[0-9.]+",x) for x in source_info[5:])
report["source_database_bytes"]=int(source_info[1])
report["source_collation_recorded"],report["source_collation_actual"]=source_info[5:]
stat=os.statvfs(root);assert stat.f_bavail*stat.f_frsize>=int(source_info[1])*2+2*1024**3
# The clone uses a temporary Docker named volume, normally on the root disk.
docker_root=Path(capture(["docker","info","--format","{{.DockerRootDir}}"]))
stat=os.statvfs(docker_root);assert stat.f_bavail*stat.f_frsize>=int(source_info[1])*3+2*1024**3
source_meta=source(metadata).splitlines()
assert source_meta[0]=="0","source has invalid indexes; review"
report["source_metadata"]=source_meta
snapshot_proc=None
created=False
vol_created=False
try:
    if a.reuse_backup:
        assert archive.is_file() and not archive.is_symlink() and archive.stat().st_uid==0 and archive.stat().st_mode&0o077==0
        expected=json.loads((root/"source-counts.json").read_text())
        assert counts("\n".join(f"{k}:{v}" for k,v in expected.items()))==expected
        digest=(root/"videoroll.dump.sha256").read_text().split()[0]
        assert sha(archive)==digest
        report["archive_sha256"]=digest
        report["archive_bytes"]=archive.stat().st_size
        report["reused_backup"]=True
        report["metadata_note"]="Source metadata rechecked at restore time; row counts from original exported snapshot"
    else:
        with log.open("ab") as err:
            snapshot_proc=subprocess.Popen(["docker","exec","-i",a.source,"sh","-c",
                'PGOPTIONS="-c default_transaction_read_only=on -c lock_timeout=5000 -c statement_timeout=60000" psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d videoroll'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,bufsize=1)
            snapshot_proc.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n");snapshot_proc.stdin.flush()
            snapshot=snapshot_proc.stdout.readline().strip()
            assert re.fullmatch(r"[0-9A-Fa-f-]+",snapshot),"snapshot not available"
            snapshot_proc.stdin.write(inventory+"\nSELECT 'COUNTS_END';\n");snapshot_proc.stdin.flush()
            lines=[]
            while True:
                line=snapshot_proc.stdout.readline()
                if line.strip()=="COUNTS_END":break
                if not line:raise RuntimeError("snapshot inventory ended early")
                lines.append(line.strip())
            expected=counts("\n".join(lines))
            assert expected.get("public.tasks",0)>0
            (root/"source-counts.json").write_text(json.dumps(expected,indent=2,sort_keys=True))
            t=time.monotonic()
            with archive.open("xb") as out:
                proc(["docker","exec",a.source,"sh","-c",
                    'PGOPTIONS="-c default_transaction_read_only=on -c lock_timeout=5000" pg_dump -U "$POSTGRES_USER" -d videoroll --format=custom --compress=1 --lock-wait-timeout=5000 --snapshot="$1"',"sh",snapshot],stdout=out,stderr=err,timeout=300)
                out.flush();os.fsync(out.fileno())
            snapshot_proc.stdin.write("ROLLBACK;\n\\q\n");snapshot_proc.stdin.flush()
            assert snapshot_proc.wait(timeout=10)==0;snapshot_proc=None
            report["dump_seconds"]=round(time.monotonic()-t,2)
        report["archive_bytes"]=archive.stat().st_size
        report["archive_sha256"]=sha(archive)
        assert report["archive_bytes"]>0
        (root/"videoroll.dump.sha256").write_text(report["archive_sha256"]+"  videoroll.dump\n")
    mark(f"consistent snapshot logical backup: {len(expected)} tables, {report['archive_bytes']} bytes, verified SHA-256")
    capture(["docker","volume","create","--label","io.videoroll.purpose=db-rehearsal",volume]);vol_created=True
    capture(["docker","run","-d","--name",name,"--network","none","--label","io.videoroll.purpose=db-rehearsal",
        "-e","POSTGRES_HOST_AUTH_METHOD=trust","-e","POSTGRES_USER=postgres",
        "--mount",f"type=volume,source={volume},target=/var/lib/postgresql",
        "--mount",f"type=bind,source={sock},target=/rehearsal-socket",
        "--mount",f"type=bind,source={archive},target=/rehearsal.dump,readonly",
        image,"-c","listen_addresses=","-c","unix_socket_directories=/var/run/postgresql,/rehearsal-socket"])
    created=True
    assert capture(["docker","inspect",name,"--format","{{.HostConfig.NetworkMode}}"])=="none"
    for i in range(90):
        if subprocess.run(["docker","exec",name,"sh","-c",'test "$(cat /proc/1/comm)" = postgres && pg_isready -U postgres -d postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
            try:
                if clone("SELECT 1;",db="postgres")=="1" and (sock/".s.PGSQL.5432").is_socket():break
            except subprocess.CalledProcessError:pass
        time.sleep(.5)
    else:raise RuntimeError("isolated database did not start")
    # Deliberately recreate the recorded collation mismatch only in clone.
    clone(f"CREATE DATABASE videoroll_test WITH TEMPLATE template0 LOCALE_PROVIDER libc LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8' COLLATION_VERSION '{source_info[5]}';",db="postgres")
    assert sha(archive)==report["archive_sha256"]
    t=time.monotonic()
    with log.open("ab") as err:
        # docker exec defaults to root, allowing access to the 0600 archive;
        # the clone postmaster still runs as its normal postgres user.
        proc(["docker","exec","--user","0",name,"pg_restore","-U","postgres","-d","videoroll_test","--exit-on-error","--no-owner","--no-privileges","--jobs",str(a.restore_jobs),"/rehearsal.dump"],stdout=err,stderr=err,timeout=a.restore_timeout)
    report["restore_seconds"]=round(time.monotonic()-t,2)
    report["restore_jobs"]=a.restore_jobs
    actual=counts(clone(inventory))
    assert actual==expected,"restored table counts differ"
    assert clone(metadata).splitlines()==source_meta,"index/constraint inventory differs"
    mark(f"restore: exact row counts for {len(actual)} tables, index/constraint counts match, {report['restore_seconds']}s")
    mismatch=clone("SELECT datcollversion,pg_database_collation_actual_version(oid) FROM pg_database WHERE datname=current_database();").split("|")
    assert mismatch==source_info[5:]
    t=time.monotonic()
    clone("REINDEX DATABASE videoroll_test;")
    assert clone(metadata).splitlines()==source_meta
    clone("ALTER DATABASE videoroll_test REFRESH COLLATION VERSION;")
    versions=clone("SELECT datcollversion,pg_database_collation_actual_version(oid) FROM pg_database WHERE datname=current_database();").split("|")
    assert versions[0]==versions[1]==source_info[6]
    assert counts(clone(inventory))==expected
    report["reindex_seconds"]=round(time.monotonic()-t,2)
    report["clone_collation_after"]=versions
    mark(f"isolated REINDEX then REFRESH: {mismatch[0]} -> {versions[0]}, {report['reindex_seconds']}s")
    # Connect ONLY through the root-private cloned unix socket; no real
    # endpoints, credentials or production storage root given to the binary.
    url="postgresql://postgres@localhost/videoroll_test?host="+quote(str(sock),safe="")
    env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH")}
    env.update(DATABASE_URL=url,VIDEOROLL_V2_STORAGE_ROOT=str(storage),
        VIDEOROLL_V2_INTERNAL_TOKEN="isolated-db-rehearsal-token-0123456789",
        INTERNAL_API_SECRET="isolated-db-rehearsal-secret",
        VIDEOROLL_V2_STANDALONE="true",VIDEOROLL_V2_NATIVE_OUTBOX_ONLY="true",
        VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH="false",VIDEOROLL_V2_COMPAT_WRITE="false",
        VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI="false",VIDEOROLL_V2_ENABLE_SAU_PUBLISH="false",
        VIDEOROLL_V2_SOURCE_SCANNER="false")
    t=time.monotonic()
    with log.open("ab") as err:
        proc([str(binary),"migrate"],env=env,stdout=err,stderr=err,timeout=300)
    report["migration_seconds"]=round(time.monotonic()-t,2)
    assert clone("SELECT count(*) FROM _sqlx_migrations WHERE success;")=="40"
    assert clone("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'vr2_%';")=="0"
    migrated=counts(clone(inventory))
    # All original physical tables and row counts preserved; extra Rust
    # infrastructure is allowed. No business API or workers are started.
    assert all(migrated.get(k)==v for k,v in expected.items()),"original row counts changed during migration"
    report["original_tables"]=len(expected)
    report["original_rows"]=sum(expected.values())
    report["migration_success_count"]=40
    report["physical_vr2_tables"]=0
    # Audit only; strict admission should still block unknown historical
    # effects. Do not adopt, modify or call any live platform.
    with log.open("ab") as err:
        q=subprocess.run([str(binary),"standalone-preflight"],env=env,stdout=subprocess.PIPE,stderr=err,text=True,timeout=60)
    out=json.loads(q.stdout[q.stdout.index("{"):])
    assert q.returncode!=0 and out["ready"] is False
    report["strict_preflight_blockers"]=out["historical_blockers"]
    mark(f"40 migrations on full restored rows; original row counts preserved; strict preflight correctly blocked")
    report["status"]="passed"
finally:
    if snapshot_proc is not None:
        snapshot_proc.terminate()
        try:snapshot_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:snapshot_proc.kill();snapshot_proc.wait()
    if created:subprocess.run(["docker","rm","-f","-v",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    if vol_created:subprocess.run(["docker","volume","rm",volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    shutil.rmtree(sock)
    (root/"report.json").write_text(json.dumps(report,indent=2,sort_keys=True))
    print(json.dumps(report,sort_keys=True),flush=True)
