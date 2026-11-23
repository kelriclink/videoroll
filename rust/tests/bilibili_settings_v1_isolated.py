"""Python Bilibili publish-defaults / Rust original-table differential."""
import os,json
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from videoroll.apps.bilibili_publisher.publish_settings_store import get_bilibili_publish_settings
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishSettingsRead

url=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in url and url.endswith('/videoroll_test')
engine=create_engine(url)
BASE='http://127.0.0.1:18080/api/v1/bilibili/publish/settings'
token=os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']
def request(method='GET',obj=None):
    data=None if obj is None else json.dumps(obj).encode()
    req=Request(BASE,data=data,method=method,headers={'x-videoroll-v2-internal-token':token,'content-type':'application/json'})
    try:r=urlopen(req,timeout=12)
    except HTTPError as e:r=e
    with r:
        raw=r.read()
        try:body=json.loads(raw)
        except ValueError:body=raw.decode(errors='replace')
        return r.status,body

def python():
    with Session(engine) as db:
        return BilibiliPublishSettingsRead(**get_bilibili_publish_settings(db)).model_dump(mode='json')
status,v=request();assert status==200,(status,v)
assert v==python(),('defaults mismatch',v,python())
with engine.begin() as db:
    db.execute(text("""INSERT INTO app_settings(key,value_json,version)
      VALUES('bilibili.publish','{"keep_me":{"foo":"bar"}}'::jsonb,1)
      ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,version=1"""))
status,v=request('PUT',{'default_meta':{'title':'跨端投稿模板','tags':['猫','猫','科学'],'desc':'来源：测试','typeid':17,'copyright':1},'upload_workers':6,'ai_activity_enabled':True})
assert status==200,(status,v)
assert v['upload_workers']==6 and v['ai_activity_enabled'] is True
assert v['default_meta']['tags']==['猫','科学'],v
assert v==python(),('PUT mismatch',v,python())
with engine.connect() as db:
    row=db.execute(text("SELECT value_json FROM app_settings WHERE key='bilibili.publish'")).scalar()
    assert row.get('keep_me')=={'foo':'bar'},row
    assert row['default_meta']['title']=='跨端投稿模板'
status,v=request('PUT',{'upload_workers':9});assert status==400,(status,v)
status,v=request('PUT',{'default_meta':{'copyright':2,'source':''}});assert status==400,(status,v)
status,v=request('PUT',{'default_meta':{'title':''}});assert status==400,(status,v)
status,v=request('PUT',{});assert status==200 and v==python(),(status,v,python())
print('PASS: original Bilibili publish defaults and native V1 GET/PUT exact Python DTO, dedup tags, unknown fields, invalid settings rejected')
