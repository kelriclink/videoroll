#!/usr/bin/env python3
"""Cross-process periodic YouTube Home scheduling: original lease, restart, backoff and manual overlap."""
import json
import os
import subprocess
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import psycopg

database=os.environ["DATABASE_URL"]
assert database.endswith("/videoroll_test") and "127.0.0.1:" in database
base=os.environ.get("VIDEOROLL_TEST_V1_BASE","http://127.0.0.1:18080/api/v1")
home="http://127.0.0.1:18195/"
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
binary=Path(os.environ.get("VIDEOROLL_TEST_BIN",str(Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2")))
assert binary.is_file(),binary
def api(method,path,body=None):
    headers={"x-videoroll-v2-internal-token":token}
    if body is not None:headers["Content-Type"]="application/json"
    req=Request(base+path,method=method,headers=headers,
                data=json.dumps(body).encode() if body is not None else None)
    try:response=urlopen(req,timeout=30)
    except HTTPError as error:response=error
    with response:
        return response.status,json.loads(response.read())
def tick():
    return subprocess.run([str(binary),"home-tick"],stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,encoding="utf-8",timeout=40,check=False)
def expected_run(expected):
    result=tick()
    assert result.returncode==0,(result.returncode,result.stdout,result.stderr)
    assert f"home_scan_run={str(expected).lower()}" in result.stdout,(expected,result.stdout)
def stored():
    with psycopg.connect(database) as connection:
        return connection.execute("SELECT value_json FROM app_settings WHERE key='youtube.settings'").fetchone()[0]
def patch(**changes):
    with psycopg.connect(database) as connection:
        row=connection.execute("SELECT value_json FROM app_settings WHERE key='youtube.settings' FOR UPDATE").fetchone()[0]
        row.update(changes)
        connection.execute("UPDATE app_settings SET value_json=%s::jsonb WHERE key='youtube.settings'",
                           (json.dumps(row),))
def cleaned():
    row=stored()
    assert not row.get("home_scan_lock_owner") and not row.get("home_scan_lock_until"),row
def old():
    return (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
def now():
    return datetime.now(timezone.utc).isoformat()

assert api("GET","/settings/youtube")[0]==200
# The preceding manual scan ran while enabled; its fresh finished time must
# not be enough to schedule another run.
before=stored()
expected_run(False)
assert stored()["home_scan_last_started_at"]==before["home_scan_last_started_at"]
print("PASS: startup tick respects stored interval after recent successful manual scan")

assert api("PUT","/settings/youtube",{"home_scan_enabled":False})[0]==200
patch(home_scan_last_finished_at=old(),home_scan_last_started_at=old())
before=stored()
expected_run(False)
assert stored()==before,"disabled tick modified original settings"
code,_=api("POST","/settings/youtube/home_scan/run")
assert code==200,"disabled automatic mode must still allow forced manual scans"
print("PASS: disabled schedule does no work or writes, but manual forced execution remains allowed")

assert api("PUT","/settings/youtube",{"home_scan_enabled":True,"home_scan_interval_minutes":60})[0]==200
patch(home_scan_last_finished_at=now())
expected_run(False)
print("PASS: original 60-minute interval suppresses premature background scans")

# Original Python can be running on the same PostgreSQL instance.
patch(home_scan_lock_owner="python-home:in-progress",
      home_scan_lock_until=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),
      home_scan_last_finished_at=old())
before=stored()
expected_run(False)
assert stored()==before
code,body=api("POST","/settings/youtube/home_scan/run")
assert code==409,(code,body)
assert stored()["home_scan_lock_owner"]=="python-home:in-progress"
print("PASS: live Python lease blocks both scheduled and manual Rust scans without stealing owner")

# Model a process that died unexpectedly: leave the stale owner until past TTL.
patch(home_scan_lock_until=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(),
      home_scan_last_finished_at=old())
expected_run(True)
cleaned()
snapshot=stored()
assert snapshot["home_scan_last_discovered_count"]==3,snapshot
assert snapshot["home_scan_last_skipped_duplicates"]==3,snapshot
assert snapshot["home_scan_last_started_count"]==0,snapshot
assert snapshot.get("home_scan_last_error") is None,snapshot
expected_run(False)
assert stored()["home_scan_last_started_at"]==snapshot["home_scan_last_started_at"]
print("PASS: fresh Rust process recovers expired Python lease, scans once, then respects interval")

# Two independent processes racing on a due row must not both start.
patch(home_scan_last_finished_at=old())
procs=[subprocess.Popen([str(binary),"home-tick"],stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,text=True) for _ in range(2)]
outputs=[process.communicate(timeout=40) for process in procs]
for process,(stdout,stderr) in zip(procs,outputs):
    assert process.returncode==0,(process.returncode,stdout,stderr)
runs=[out[0] for out in outputs]
assert sum("home_scan_run=true" in output for output in runs)==1,runs
assert sum("home_scan_run=false" in output for output in runs)==1,runs
cleaned()
print("PASS: two worker processes atomically agree on one due scan")

# An upstream error should not hammer YouTube on each 60-second scheduler tick.
with urlopen(Request(home+"mock/enable-failure",method="POST"),timeout=3) as r:assert r.status==200
try:
    patch(home_scan_last_finished_at=old())
    result=tick()
    assert result.returncode!=0,(result.returncode,result.stdout,result.stderr)
    snapshot=stored()
    assert snapshot["home_scan_last_error"] and snapshot["home_scan_last_finished_at"],snapshot
    cleaned()
    expected_run(False)
    assert stored()["home_scan_last_started_at"]==snapshot["home_scan_last_started_at"]
finally:
    with urlopen(Request(home+"mock/disable-failure",method="POST"),timeout=3) as r:assert r.status==200
print("PASS: failed scheduled scan persists original error, releases lease and waits until next interval")

# No original settings row must not bootstrap automatic recommendations.
with psycopg.connect(database) as connection:
    connection.execute("DELETE FROM app_settings WHERE key='youtube.settings'")
expected_run(False)
with psycopg.connect(database) as connection:
    count=connection.execute("SELECT count(*) FROM app_settings WHERE key='youtube.settings'").fetchone()[0]
assert count==0,count
print("PASS: absent original settings row does not create defaults or run background scan")
