#!/usr/bin/env python3
"""Home recommendations V1: real Rust HTTP, loopback Innertube, original DB and duplicate fence."""
import json,os,uuid,time
from datetime import datetime,timedelta,timezone
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from pathlib import Path
import psycopg

base="http://127.0.0.1:18080/api/v1"
url="http://127.0.0.1:18195/"
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
db_url=os.environ["DATABASE_URL"]
assert db_url.endswith("/videoroll_test") and "127.0.0.1:" in db_url
assert os.environ["VIDEOROLL_V2_TEST_YOUTUBE_HOME_BASE"].rstrip("/") == url.rstrip("/")
# A prior aborted smoke run may have left a private cookie file in this reused
# build target. The isolated test must never inherit credentials from it.
storage=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])
assert storage.name=="v2-smoke-objects" and "rust/target" in storage.as_posix(),storage
(storage/".videoroll-private"/"youtube-cookies.txt").unlink(missing_ok=True)
def api(method,path,payload=None,auth=True):
    headers={"x-videoroll-v2-internal-token":token} if auth else {}
    data=json.dumps(payload).encode() if payload is not None else None
    if data is not None:headers["Content-Type"]="application/json"
    try:r=urlopen(Request(base+path,data=data,headers=headers,method=method),timeout=60)
    except HTTPError as err:r=err
    with r:
        raw=r.read()
        try:body=json.loads(raw)
        except ValueError:body=raw.decode(errors="replace")
        return r.status,body
def current():
    with psycopg.connect(db_url) as conn:
        return conn.execute("SELECT value_json FROM app_settings WHERE key='youtube.settings'").fetchone()[0]
def set_lock(owner,until):
    with psycopg.connect(db_url) as conn:
        conn.execute("""UPDATE app_settings SET value_json=
          jsonb_set(jsonb_set(value_json,'{home_scan_lock_owner}',to_jsonb(%s::text)),
          '{home_scan_lock_until}',to_jsonb(%s::text))
          WHERE key='youtube.settings'""",(owner,until))
cookies="# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tisolated-home-token\n.youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tisolated-visitor\n"
path="/settings/youtube/home_scan/run"
assert api("POST",path,auth=False)[0]==401
code,err=api("POST",path)
assert code==400,(code,err)
assert "youtube cookies" in err["detail"],err
assert current().get("home_scan_lock_owner") is None
print("PASS: original Home scan requires authenticated caller and configured private Netscape cookies")

code,settings=api("PUT","/settings/youtube",{
    "cookies_txt":cookies,
    "home_scan_enabled":False, # manual force must run even if scheduler is off
    "home_scan_limit":4,
    "home_scan_interval_minutes":60,
    "home_scan_long_videos_only":True,
    "home_scan_min_duration_seconds":180,
})
assert code==200,(code,settings)
assert settings["home_scan_enabled"] is False and settings["home_scan_limit"]==4
assert settings["home_scan_long_videos_only"] and settings["home_scan_min_duration_seconds"]==180
assert settings["cookies_set"] is True
assert "isolated-home-token" not in json.dumps(settings) and "isolated-home-token" not in json.dumps(current())
print("PASS: original Home config accepts scan options and stores Netscape credentials outside PostgreSQL")

existing_id=uuid.uuid4()
with psycopg.connect(db_url) as conn:
    conn.execute("""INSERT INTO tasks(id,source_type,source_url,source_license,status)
       VALUES (%s,'youtube','https://www.youtube.com/watch?v=ABCDEF12345','authorized','created')""",(existing_id,))
    conn.execute("""INSERT INTO ingested_videos(id,platform,source_id,task_id)
       VALUES(%s,'youtube','ABCDEF12345',%s)""",(uuid.uuid4(),existing_id))

future=(datetime.now(timezone.utc)+timedelta(minutes=45)).isoformat()
set_lock("python-home:already-running",future)
code,locked=api("POST",path)
assert code==409,(code,locked)
code,settings=api("GET","/settings/youtube")
assert code==200 and settings["home_scan_running"] is True
with psycopg.connect(db_url) as conn:
    conn.execute("""UPDATE app_settings SET value_json=value_json-'home_scan_lock_owner'-'home_scan_lock_until'
      WHERE key='youtube.settings'""")
print("PASS: original Python-held home scan lock rejects concurrent Rust request with 409")

code,result=api("POST",path)
assert code==200,(code,result)
assert result["discovered_count"]==4,result
assert result["skipped_duplicates"]==1,result
assert result["failed_count"]==0,result
assert len(result["created_task_ids"])==3,result
assert len(result["started_pipeline_job_ids"])==3,result
assert result["candidate_count"]==6,result
assert result["explicit_shorts_count"]==1,result
assert result["known_duration_count"]==4,result
assert result["unknown_duration_count"]==1,result
assert result["below_min_duration_count"]==1,result
assert result["kept_unknown_duration_count"]==1,result
assert result["eligible_count"]==4,result
assert result["min_duration_seconds"]==180,result
assert all(x.startswith("https://www.youtube.com/watch?v=") for x in result["sample_urls"]),result
assert len(result["sample_urls"])==4
with psycopg.connect(db_url) as conn:
    rows=conn.execute("""SELECT id,created_by,origin_kind,origin_run_id FROM tasks
          WHERE id=ANY(%s::uuid[])""",(result["created_task_ids"],)).fetchall()
    assert len(rows)==3
    for task_id,created_by,origin_kind,run_id in rows:
        assert origin_kind=="youtube_home_scan",(task_id,origin_kind)
        assert created_by==f"youtube_home_scan;auto_publish=;run_id={run_id}",created_by
        assert run_id
    db_created=conn.execute("""SELECT count(*) FROM ingested_videos
       WHERE platform='youtube' AND source_id=ANY(%s::text[])""",
       (["ABCDEF12345","UNKNOWN1234","FRESHXX1234","LATERXX1234"],)).fetchone()[0]
    assert db_created==4,db_created
persist=current()
assert persist.get("home_scan_lock_owner") is None
assert persist["home_scan_last_discovered_count"]==4,persist
assert persist["home_scan_last_started_count"]==3,persist
assert persist["home_scan_last_skipped_duplicates"]==1,persist
assert persist["home_scan_last_candidate_count"]==6,persist
assert persist["home_scan_last_kept_unknown_duration_count"]==1,persist
assert persist.get("home_scan_last_error") is None
print("PASS: real native YouTube Browse pagination and long-only stats; duplicate protection and original Task+Workflow source attribution")

code,again=api("POST",path)
assert code==200 and again["discovered_count"]==4,(code,again)
assert again["skipped_duplicates"]==4 and not again["created_task_ids"] and not again["started_pipeline_job_ids"],again
assert current()["home_scan_last_started_count"]==0
assert current().get("home_scan_lock_owner") is None
print("PASS: repeated force scan deduplicates against original ingested_videos and never redispatches")

with urlopen(Request(url+"mock/enable-failure",method="POST"),timeout=4) as control:
    assert control.status==200
code,upstream_failure=api("POST",path)
assert code==502,(code,upstream_failure)
persist=current()
assert persist.get("home_scan_lock_owner") is None and persist.get("home_scan_lock_until") is None,persist
assert persist["home_scan_last_error"] and persist["home_scan_last_finished_at"],persist
with urlopen(Request(url+"mock/disable-failure",method="POST"),timeout=4) as control:
    assert control.status==200
print("PASS: upstream Browse failure returns 502, updates original last_error and releases cross-runtime lease")

code,settings=api("PUT","/settings/youtube",{"home_scan_enabled":True,"home_scan_limit":3,
    "home_scan_long_videos_only":False,"home_scan_min_duration_seconds":240})
assert code==200 and settings["home_scan_enabled"] is True and settings["home_scan_limit"]==3,settings
code,settings=api("GET","/settings/youtube")
assert settings["home_scan_enabled"] is True and settings["home_scan_running"] is False
code,last=api("POST",path)
assert code==200 and last["discovered_count"]==3,(code,last)
assert last["explicit_shorts_count"]==1,last
assert last["skipped_duplicates"]==2,last
assert len(last["created_task_ids"])==1,last
print("PASS: GET/PUT original Home settings and manual scan mode reflect selected filters")
