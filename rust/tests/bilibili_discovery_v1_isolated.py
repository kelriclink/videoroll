#!/usr/bin/env python3
"""Native Bilibili dashboard discovery: Rust HTTP + Python parser differential."""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from videoroll.apps.orchestrator_api.services.bilibili_discovery import (
    parse_ranking, parse_activities,
)

assert os.environ["DATABASE_URL"].endswith("/videoroll_test")
assert "127.0.0.1:" in os.environ["DATABASE_URL"]
assert os.environ.get("VIDEOROLL_V2_TEST_BILI_DISCOVERY_BASE")=="http://127.0.0.1:18196/"
assert os.environ.get("VIDEOROLL_V2_ALLOW_TEST_BILI_DISCOVERY_LOOPBACK")=="true"

API="http://127.0.0.1:18080/api/v1"
MOCK="http://127.0.0.1:18196"
TOKEN=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
def request(url,method="GET",body=None,token=None):
    headers={}
    if token is not None:headers["x-videoroll-v2-internal-token"]=token
    req=Request(url,method=method,headers=headers,data=body)
    try:response=urlopen(req,timeout=20)
    except HTTPError as err:response=err
    with response:
        raw=response.read()
        try:return response.status,json.loads(raw)
        except ValueError:return response.status,raw.decode(errors="replace")
def api(path="/bilibili/discovery",port=18080,token=TOKEN):
    return request(f"http://127.0.0.1:{port}/api/v1"+path,token=token)
def mode(name):
    status,result=request(MOCK+"/__mock__/mode/"+name,method="POST",body=b"")
    assert status==200,(status,result)
def counts():
    status,result=request(MOCK+"/__mock__/status")
    assert status==200,result
    return result["counts"]
def child(port):
    env=os.environ.copy()
    env["VIDEOROLL_V2_BIND"]="127.0.0.1:"+str(port)
    executable=str(Path(__file__).resolve().parents[1]/"target/debug/videoroll-v2")
    p=subprocess.Popen([executable,"api"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    for _ in range(50):
        if p.poll() is not None:raise AssertionError("Rust child crashed: "+p.stderr.read().decode())
        try:
            status,_=request(f"http://127.0.0.1:{port}/health",token=None)
            if status==200:break
        except URLError:time.sleep(.15)
    else:
        p.kill();raise AssertionError("Rust child did not start")
    return p
def stop(p):
    p.terminate()
    try:p.wait(timeout=5)
    except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)

for attempt in range(35):
    try:
        if counts() is not None:break
    except URLError:time.sleep(.2)
else:raise AssertionError("isolated Bilibili mock did not start")
mode("healthy")
assert api(token=None)[0]==401
status,result=api()
assert status==200,(status,result)
assert set(result)=={"ranking","activities","note"},result
assert result["note"]=="公开活动列表仅供浏览，不代表账号有报名或投稿资格。"
assert result["ranking"]["source"]=="排行榜"
assert result["ranking"]["stale"] is False
assert result["ranking"]["error"] is None
assert result["ranking"]["fetched_at"]
assert result["activities"]["source"]=="公开活动"
assert result["activities"]["stale"] is False
assert result["activities"]["error"] is None
expected_rank=parse_ranking(request(MOCK+"/__mock__/ranking")[1])
assert result["ranking"]["items"]==expected_rank,(result["ranking"]["items"],expected_rank)
pages=request(MOCK+"/__mock__/activities1")[1]
pages["data"]["list"]+=request(MOCK+"/__mock__/activities2")[1]["data"]["list"]
now=int(time.time())
expected_activities=parse_activities(pages,now=now)
assert result["activities"]["items"]==expected_activities,(result["activities"]["items"],expected_activities)
assert [row["id"] for row in expected_activities]==[101,104],expected_activities
assert all(row["eligibility"]=="unknown" and "mission_id" not in row and "topic_id" not in row
           for row in result["activities"]["items"]),result
assert counts()=={"ranking":1,"popular":0,"activities1":1,"activities2":1},counts()
print("PASS: original Python ranking/activity parser == native Rust JSON payload, source labels and public eligibility caution")

before=counts()
for suffix in ("","?refresh=false","?refresh=true"):
    code,again=api("/bilibili/discovery"+suffix)
    assert code==200 and again==result,(code,again)
assert counts()==before,(before,counts())
print("PASS: 300-second cache and 60-second forced refresh cooldown make no duplicate upstream requests")

# A new Rust process starts with empty cache. Ranking returns code=-352,
# causing original Python fallback to the popular feed; activity page 2 fails
# and must not erase the valid first-page activity.
mode("fallback")
process=child(18180)
try:
    code,fallback=api(port=18180)
    assert code==200,(code,fallback)
    assert fallback["ranking"]["source"]=="热门综合",fallback
    assert fallback["ranking"]["items"]==expected_rank,fallback
    assert fallback["activities"]["items"]==parse_activities(
        request(MOCK+"/__mock__/activities1")[1],now=now),fallback
    assert fallback["activities"]["error"] is None
    current=counts()
    assert current["ranking"]==2 and current["popular"]==1,current
    assert current["activities1"]==2 and current["activities2"]==2,current
    print("PASS: ranking API -352 falls back to popular; activity page 2 HTTP 503 preserves page one")
finally:stop(process)

mode("fail")
process=child(18181)
try:
    code,failed=api(port=18181)
    assert code==200,(code,failed)
    for key in ("ranking","activities"):
        row=failed[key]
        assert row["items"]==[] and row["fetched_at"] is None and row["source"] is None,row
        assert row["stale"] is False and row["error"]=="B站数据暂时不可用",row
    print("PASS: both public ranking feeds and activity feed failing returns original non-crashing empty status")
finally:
    stop(process)
    mode("healthy")
