#!/usr/bin/env python3
"""V1 YouTube source parity: positive CRUD, idempotence, locks, async scan.
Only disposable PostgreSQL and yt-dlp mock; no live YouTube requests.
"""
import json
import os
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.environ.get("VIDEOROLL_TEST_V1_BASE","http://127.0.0.1:18080/api/v1")
TOKEN = os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
CONTAINER = os.environ["container_name"]
HEADERS = {"x-videoroll-v2-internal-token": TOKEN, "content-type":"application/json"}
UC = "UC12345678901234567890"
PL = "PL12345678901234567890"

def req(method, path, body=None, authenticated=True):
    assert path.startswith("/")
    headers = HEADERS.copy() if authenticated else {"content-type":"application/json"}
    raw = json.dumps(body).encode() if body is not None else None
    q = Request(BASE+path, method=method, data=raw, headers=headers)
    try:
        with urlopen(q,timeout=9) as res:
            return res.status,json.loads(res.read())
    except HTTPError as err:
        return err.code,json.loads(err.read())

def pg(sql):
    return subprocess.run(["docker","exec",CONTAINER,"psql","-X","-At","-v","ON_ERROR_STOP=1",
      "-U","postgres","-d","videoroll_test","-c",sql],check=True,
      stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()

def check_equal(row):
    expected = {
      "id","source_type","source_id","source_url","display_name","license",
      "proof_url","enabled","scan_interval_minutes","scan_limit","auto_process",
      "scan_running","last_scan_started_at","last_scan_finished_at",
      "last_scan_discovered_count","last_scan_created_count",
      "last_scan_started_pipeline_count","last_scan_skipped_duplicates",
      "last_scan_error","last_nonempty_scan_finished_at",
      "last_nonempty_discovered_count","total_scan_count","total_failed_scan_count",
      "total_created_count","total_started_pipeline_count","created_at","updated_at"
    }
    assert set(row)==expected, (set(row)-expected,expected-set(row))

status,body=req("GET","/health",authenticated=False)
assert status==200 and body=={"status":"ok"},(status,body)
assert req("GET","/youtube/sources",authenticated=False)[0]==401

status,row=req("POST","/youtube/sources",{"source_type":"channel","source_id":UC,
 "license":"authorized","proof_url":"https://example.test/license",
 "enabled":True,"auto_process":False,"scan_limit":1})
assert status==200,(status,row)
check_equal(row)
uid=row["id"]
assert row["source_url"]==f"https://www.youtube.com/channel/{UC}"
assert row["license"]=="authorized" and row["proof_url"]=="https://example.test/license"
assert row["scan_limit"]==1 and row["auto_process"] is False

status,row=req("POST","/youtube/sources",{"source_url":f"https://www.youtube.com/channel/{UC}/videos",
 "license":"cc","scan_limit":2,"auto_process":False})
assert status==200 and row["id"]==uid and row["license"]=="cc",(status,row)
status,listing=req("GET","/youtube/sources")
assert status==200 and sum(x["id"]==uid for x in listing)==1
assert len(listing)>=1
status,row=req("PATCH",f"/youtube/sources/{uid}",
 {"license":"own","proof_url":None,"display_name":"  Youtube test title  ",
  "scan_interval_minutes":15,"scan_limit":2,"auto_process":False})
assert status==200,(status,row)
assert row["proof_url"] is None and row["display_name"]=="Youtube test title",row
assert row["scan_interval_minutes"]==15 and row["license"]=="own",row
assert req("PATCH",f"/youtube/sources/{uid}",{"scan_limit":0})[0]==400
assert req("POST","/youtube/sources",{"source_url":"http://127.0.0.1/admin"})[0]==400
assert req("POST","/youtube/sources",{"source_type":"playlist",
  "source_url":f"https://www.youtube.com/channel/{UC}"})[0]==400

# Force an unexpired claim to check duplicate-scan 409 deterministically.
pg(f"UPDATE youtube_sources SET scan_lock_owner='v1-source-test',"
   f"scan_lock_until=now()+interval '10 minutes' WHERE id='{uid}'")
assert req("POST",f"/youtube/sources/{uid}/scan/start",{})[0]==409
pg(f"UPDATE youtube_sources SET scan_lock_owner=NULL,scan_lock_until=NULL WHERE id='{uid}'")

status,accepted=req("POST",f"/youtube/sources/{uid}/scan/start",
 {"limit":2,"auto_process":False})
assert status==202 and accepted["accepted"] is True and accepted["scan_started_at"],(status,accepted)
done=False
for _ in range(70):
    # The original Python contract only exposes list, patch and delete,
    # not GET /youtube/sources/{id}. Poll the actual frontend list contract.
    status,listing=req("GET","/youtube/sources")
    assert status==200,(status,listing)
    row=next(item for item in listing if item["id"]==uid)
    if row["last_scan_finished_at"] and not row["scan_running"]:
        done=True
        break
    time.sleep(.1)
assert done,row
assert row["last_scan_discovered_count"]==2 and row["last_scan_error"] is None,row
assert row["total_scan_count"]>=1,row
assert row["total_started_pipeline_count"]==0,row

status,scan=req("POST",f"/youtube/sources/{uid}/scan",
 {"limit":1,"auto_process":False})
assert status==200,(status,scan)
assert set(scan)=={"discovered_count","created_task_ids","skipped_duplicates","started_pipeline_job_ids"}
assert scan["discovered_count"]==2 and len(scan["started_pipeline_job_ids"])==0,scan
status,since_scan=req("POST","/youtube/scan",{"source_type":"channel","source_id":UC,
  "since":"2026-11-20T00:00:00Z","limit":2,"auto_process":False})
assert status==200 and since_scan["discovered_count"]==0 and not since_scan["created_task_ids"],(status,since_scan)
status,scan=req("POST","/youtube/scan",
 {"source_type":"channel","source_id":UC,"limit":1,"auto_process":False})
assert status==200 and scan["discovered_count"]==2,(status,scan)

# A playlist with known first entries must still create a later video with
# limit=1, rather than repeatedly stopping at the already-ingested first item.
DEEP="UC99999999999999999999"
status,deep=req("POST","/youtube/sources",{"source_type":"channel",
    "source_id":DEEP,"license":"authorized","scan_limit":1,"auto_process":False})
assert status==200 and deep["source_id"]==DEEP,(status,deep)
status,deep_scan=req("POST",f"/youtube/sources/{deep['id']}/scan",{"limit":1,"auto_process":False})
assert status==200 and deep_scan["discovered_count"]==3,(status,deep_scan)
assert deep_scan["skipped_duplicates"]>=2 and len(deep_scan["created_task_ids"])==1,deep_scan
assert deep_scan["started_pipeline_job_ids"]==[],deep_scan
# Explicit since excludes old/undated videos but retains the dated late item.
status,deep_since=req("POST","/youtube/scan",{"source_type":"channel",
    "source_id":DEEP,"since":"2026-11-20T00:00:00Z","limit":1,"auto_process":False})
assert status==200 and deep_since["discovered_count"]==1,(status,deep_since)
assert deep_since["created_task_ids"]==[] and deep_since["skipped_duplicates"]==1,deep_since
assert req("DELETE",f"/youtube/sources/{deep['id']}")[0]==200
print("PASS: full feed enumeration skips already-ingested lead entries and respects date cutoff")

status,pl=req("POST","/youtube/sources",{"source_url":PL,"license":"authorized",
 "enabled":False})
assert status==200 and pl["source_type"]=="playlist",(status,pl)
assert req("POST",f"/youtube/sources/{pl['id']}/scan/start",{})[0]==400
status,deleted=req("DELETE",f"/youtube/sources/{pl['id']}")
assert status==200 and deleted=={"ok":True},(status,deleted)
status,deleted=req("DELETE",f"/youtube/sources/{uid}")
assert status==200 and deleted=={"ok":True},(status,deleted)
status,listing=req("GET","/youtube/sources")
assert status==200 and all(item["id"]!=uid for item in listing)
assert req("DELETE",f"/youtube/sources/{uid}")[0]==404
# Historical Python home-scan counters are read-only. The V2 scanner is NOT
# activated by stale legacy config; V2 must not forge an active scan.
pg("""INSERT INTO app_settings(key,value_json) VALUES
 ('youtube.settings', '{"home_scan_last_candidate_count":17,
  "home_scan_last_started_at":"2026-11-20T12:00:00Z",
  "home_scan_last_failed_count":2,
  "home_scan_last_sample_urls":["https://www.youtube.com/watch?v=0123456789A"]}'::jsonb)
 ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json""")
status,settings=req("GET","/settings/youtube")
assert status==200 and settings["home_scan_last_candidate_count"]==17,(status,settings)
assert settings["home_scan_last_failed_count"]==2,settings
assert settings["home_scan_last_started_at"]=="2026-11-20T12:00:00Z"
assert settings["home_scan_enabled"] is False and settings["home_scan_running"] is False
assert settings["home_scan_last_sample_urls"]==["https://www.youtube.com/watch?v=0123456789A"]
assert settings["home_scan_last_eligible_count"]==0
print("PASS: V1 YouTube settings historical source counters read from existing records without changing V2 scanner state")

print("PASS: V1 Python-shaped YouTube source CRUD, dedupe, source schema, locks, async/sync scans, auth and deletion")

# Dedicated independent channel verifies Videos+Shorts merge, known-date
# ordering, proof and original metadata, partial failure and due semantics.
status,src=req("POST","/youtube/sources",{"source_type":"channel","source_id":"UC88888888888888888888","license":"authorized","proof_url":"https://example.test/permission","enabled":True,"auto_process":True,"scan_limit":3})
assert status==200,(status,src)
sid=src["id"]
pg("""CREATE FUNCTION reject_scan_fixture() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN IF NEW.source_url LIKE '%SCANVIDEO01' THEN RAISE EXCEPTION 'fixture second entry fails'; END IF; RETURN NEW; END$$;
CREATE TRIGGER reject_scan_fixture BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION reject_scan_fixture()""")
status,body=req("POST",f"/youtube/sources/{sid}/scan",{})
assert status>=400,(status,body)
pg("DROP TRIGGER reject_scan_fixture ON tasks; DROP FUNCTION reject_scan_fixture()")
assert pg(f"SELECT concat_ws('|',last_scan_discovered_count,last_scan_created_count,last_scan_started_pipeline_count,total_scan_count,total_failed_scan_count,scan_lock_owner IS NULL) FROM youtube_sources WHERE id='{sid}'")=="3|1|1|0|1|t"
assert pg("SELECT concat_ws('|',t.source_proof_url,t.status::text,t.origin_kind,t.created_by LIKE 'auto_youtube;auto_publish=;run_id=%',i.published_at::date) FROM ingested_videos i JOIN tasks t ON t.id=i.task_id WHERE i.source_id='SCANSHORT01'")=="https://example.test/permission|ingested|auto_youtube|t|2026-11-25"
status,body=req("POST",f"/youtube/sources/{sid}/scan",{"auto_process":False})
assert status==200 and len(body["created_task_ids"])==2,(status,body)
assert pg("SELECT concat_ws('|',t.source_proof_url,t.status::text,t.origin_kind IS NULL,t.created_by IS NULL,t.origin_run_id IS NULL,i.published_at::date) FROM ingested_videos i JOIN tasks t ON t.id=i.task_id WHERE i.source_id='SCANVIDEO01'")=="https://example.test/permission|ingested|t|t|t|2026-11-23"
assert pg(f"SELECT concat_ws('|',last_scan_discovered_count,last_scan_created_count,last_scan_skipped_duplicates,total_scan_count,total_failed_scan_count,total_created_count,total_started_pipeline_count) FROM youtube_sources WHERE id='{sid}'")=="3|2|1|1|1|3|1"
print("PASS: both tabs, stable date order, source proof, original origin/ingested status, partial failure and historical counters")
if os.environ.get("VIDEOROLL_TEST_BIN"):
    bin=os.environ["VIDEOROLL_TEST_BIN"]
    env={**os.environ,"VIDEOROLL_V2_SOURCE_SCANNER":"true"}
    pg(f"UPDATE youtube_sources SET last_scan_started_at=now()-interval '2 days',last_scan_finished_at=now() WHERE id='{sid}'")
    before=pg(f"SELECT total_scan_count FROM youtube_sources WHERE id='{sid}'")
    result=subprocess.check_output([bin,"source-tick"],env=env,text=True)
    assert "source_scans_completed=0" in result,result
    assert pg(f"SELECT total_scan_count FROM youtube_sources WHERE id='{sid}'")==before
    pg(f"UPDATE youtube_sources SET last_scan_finished_at=now()-interval '2 days' WHERE id='{sid}'")
    a=subprocess.Popen([bin,"source-tick"],env=env,stdout=subprocess.PIPE,text=True)
    b=subprocess.Popen([bin,"source-tick"],env=env,stdout=subprocess.PIPE,text=True)
    outputs=[p.communicate(timeout=30)[0] for p in (a,b)]
    assert a.returncode==b.returncode==0,outputs
    assert sum("source_scans_completed=1" in x for x in outputs)==1 and sum("source_scans_completed=0" in x for x in outputs)==1,outputs
    assert pg(f"SELECT total_scan_count FROM youtube_sources WHERE id='{sid}'")==str(int(before)+1)
    print("PASS: completed-time interval and atomic scheduled claim across two processes")
assert req("DELETE",f"/youtube/sources/{sid}")[0]==200

status,src=req("POST","/youtube/sources",{"source_type":"channel","source_id":"UC77777777777777777777","license":"authorized","enabled":True,"auto_process":True,"scan_limit":3})
assert status==200,(status,src)
sid=src["id"]
pg("""CREATE FUNCTION reject_pipeline_fixture() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN IF EXISTS(SELECT 1 FROM tasks WHERE id=NEW.task_id AND source_url LIKE '%SCANFAIL002') THEN RAISE EXCEPTION 'fixture enqueue fails'; END IF; RETURN NEW; END$$;
CREATE TRIGGER reject_pipeline_fixture BEFORE INSERT ON pipeline_runs FOR EACH ROW EXECUTE FUNCTION reject_pipeline_fixture()""")
status,body=req("POST",f"/youtube/sources/{sid}/scan",{})
pg("DROP TRIGGER reject_pipeline_fixture ON pipeline_runs; DROP FUNCTION reject_pipeline_fixture()")
assert status==200 and len(body["created_task_ids"])==3 and len(body["started_pipeline_job_ids"])==2,(status,body)
assert pg(f"SELECT concat_ws('|',last_scan_discovered_count,last_scan_created_count,last_scan_started_pipeline_count,total_scan_count,total_failed_scan_count,last_scan_error LIKE '%SCANFAIL002%') FROM youtube_sources WHERE id='{sid}'")=="3|3|2|0|1|t"
assert pg("SELECT concat_ws('|',t.status::text,(SELECT count(*) FROM vr2_workflows w WHERE w.task_id=t.id)) FROM ingested_videos i JOIN tasks t ON t.id=i.task_id WHERE i.source_id='SCANFAIL002'")=="ingested|0"
assert req("DELETE",f"/youtube/sources/{sid}")[0]==200
print("PASS: failed workflow enqueue preserves task and dedupe, scans subsequent entries, original failed-scan counters")
