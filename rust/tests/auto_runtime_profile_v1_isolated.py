"""V1 automatic intake and independent dubbing policy."""
import json,os
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from urllib.parse import urlsplit
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import psycopg

uri=os.environ["DATABASE_URL"]
assert urlsplit(uri).hostname=="127.0.0.1" and urlsplit(uri).path=="/videoroll_test"
def db():return psycopg.connect(uri)
def call(path,body,method="POST"):
    req=Request("http://127.0.0.1:18080/api/v1"+path,data=json.dumps(body).encode(),method=method,headers={"content-type":"application/json","x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]})
    try:r=urlopen(req,timeout=30)
    except HTTPError as e:r=e
    with r:return r.status,json.loads(r.read())
def flow(t):
    with db() as c:return c.execute("SELECT id,configuration FROM vr2_workflows WHERE task_id=%s ORDER BY generation",(t,)).fetchall()
# Concurrent first saves must merge rather than lose one of the edits.
with db() as c:
    c.execute("DELETE FROM app_settings WHERE key='subtitle.auto_profile'")
with ThreadPoolExecutor(max_workers=2) as pool:
    replies=list(pool.map(lambda p:call("/subtitle/auto/profile",p,"PUT"),[{"asr_engine":"auto","asr_model":"fixture-asr"},{"translate_enable_summary":False,"youtube_subtitle_mode":"auto_source"}]))
assert all(code==200 for code,_ in replies),replies
def persisted():
    with db() as c:return c.execute("SELECT value_json FROM app_settings WHERE key='subtitle.auto_profile'").fetchone()[0]
assert persisted()["asr_model"]=="fixture-asr" and persisted()["translate_enable_summary"] is False
assert persisted()["youtube_subtitle_mode"]=="auto_source" and persisted()["prefer_youtube_subtitles"] is True
assert call("/subtitle/auto/profile",{"prefer_youtube_subtitles":False},"PUT")[1]["youtube_subtitle_mode"]=="off"
assert call("/subtitle/auto/profile",{"youtube_subtitle_mode":"auto_source","prefer_youtube_subtitles":False},"PUT")[1]["prefer_youtube_subtitles"] is True
assert call("/subtitle/auto/profile",{"asr_model":""},"PUT")[1]["asr_model"] is None
assert persisted()["asr_model"] is None
saved=persisted()
for bad in ({"asr_engine":"invalid"},{"youtube_subtitle_mode":"invalid"},{"translate_enable_summary":"false"},{"asr_model":"x\n"},{"asr_model":"a"*1025}):
    assert call("/subtitle/auto/profile",bad,"PUT")[0]==400
    assert persisted()==saved
assert call("/subtitle/auto/profile",{"formats":["srt"],"asr_model":"must-not-save"},"PUT")[0]==409
assert persisted()==saved
print("PASS: original ASR/summary/caption profile writes, concurrent first-save merge, validation and atomic rejection")
with db() as c:
    c.execute("INSERT INTO app_settings(key,value_json) VALUES('subtitle.auto_profile',%s::jsonb) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",(json.dumps({"auto_publish":False,"auto_publish_platforms":["douyin"],"auto_dub_p2":False}),))
for video,switch in (("AutoCfg0001",True),("AutoCfg0002",False),("AutoCfg0003",None)):
    payload={"url":"https://www.youtube.com/watch?v="+video,"license":"authorized","auto_publish":switch,"proof_url":"https://example.org/proof"}
    code,body=call("/auto/youtube",payload)
    assert code==201,(code,body)
    t=body["task_id"]
    assert flow(t)[0][1]=={"legacy_auto_runtime_profile":True}
    with db() as c:
        assert c.execute("SELECT source_proof_url FROM tasks WHERE id=%s",(t,)).fetchone()[0]==payload["proof_url"]
    code,repeat=call("/auto/youtube",{**payload,"auto_publish":not switch})
    assert code==200 and repeat["deduped"] and repeat["task_id"]==t
    assert len(flow(t))==1
    assert call("/tasks/"+t+"/actions/auto_publish",{})[1]["status"]=="skipped"
    assert call("/tasks/"+t+"/actions/auto_dubbing",{})[1]["status"]=="skipped"
# Dubbing can be independently enabled without publication targets.
code,profile=call("/subtitle/auto/profile",{"auto_dub_p2":True},"PUT")
assert code==200 and profile["auto_dub_p2"] is True and profile["auto_publish"] is False,(code,profile)
# No source/subtitles yet, so the dubbing action must reach its real preconditions,
# not incorrectly report that the automatic switch is disabled.
code,detail=call("/tasks/"+t+"/actions/auto_dubbing",{})
assert code==409 and "subtitles" in str(detail),(code,detail)
assert call("/subtitle/auto/profile",{"auto_dub_p2":"true"},"PUT")[0]==400
assert call("/subtitle/auto/profile",{"auto_dub_p2":False},"PUT")[0]==200
t=uuid4()
with db() as c:
    c.execute("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(%s,'youtube','https://www.youtube.com/watch?v=AutoCfg0004','authorized','created','rust')",(t,))
code,body=call("/tasks/"+str(t)+"/actions/auto_youtube_start",{})
assert code==200,(code,body)
assert flow(t)[0][1]=={"legacy_auto_runtime_profile":True}
with db() as c:
    origin=c.execute("SELECT runtime_origin,origin_kind,origin_run_id,created_by FROM tasks WHERE id=%s",(t,)).fetchone()
    assert origin[:2]==("youtube_auto","youtube_task_restart") and origin[2] and "auto_publish=;" in origin[3],origin
assert call("/tasks/"+str(t)+"/actions/auto_youtube_start",{})[0]==200 and len(flow(t))==1
blocked=uuid4()
with db() as c:
    c.execute("INSERT INTO tasks(id,source_type,source_url,source_license,status,runtime_owner) VALUES(%s,'youtube','https://www.youtube.com/watch?v=AutoCfg0005','authorized','downloaded','rust')",(blocked,))
    c.execute("INSERT INTO subtitle_jobs(id,task_id,status,request_json) VALUES(%s,%s,'running','{}')",(uuid4(),blocked))
assert call("/tasks/"+str(blocked)+"/actions/auto_youtube_start",{})[0]==409 and flow(blocked)==[]
with db() as c:
    assert c.execute("SELECT count(*) FROM publish_jobs").fetchone()[0]==0
    assert c.execute("SELECT count(*) FROM publish_batches").fetchone()[0]==0
print("PASS: V1 intake runtime policy, atomic proof, dedupe, explicit restart origin, independent dubbing setting and preconditions, historical work protection")
