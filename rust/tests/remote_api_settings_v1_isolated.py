"""V1 /settings/api exact response and Python PBKDF2 interoperability."""
import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json
from sqlalchemy import create_engine, text
from videoroll.apps.orchestrator_api.admin_auth_store import verify_password_hash

url = os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in url and url.endswith('/videoroll_test')
engine=create_engine(url)
BASE='http://127.0.0.1:18080/api/v1/settings/api'
token=os.environ['VIDEOROLL_V2_INTERNAL_TOKEN']
def request(method='GET',obj=None):
    data=None if obj is None else json.dumps(obj).encode()
    req=Request(BASE,data=data,method=method,headers={
        'x-videoroll-v2-internal-token':token,
        'content-type':'application/json'})
    try:r=urlopen(req,timeout=12)
    except HTTPError as e:r=e
    with r:
        payload=r.read()
        try:body=json.loads(payload)
        except (TypeError,ValueError):body=payload.decode(errors='replace')
        return r.status,body
with engine.begin() as db:
    db.execute(text("""INSERT INTO app_settings(key,value_json,version)
        VALUES('remote.api','{"other_config":"must survive"}'::jsonb,7)
        ON CONFLICT(key) DO UPDATE SET value_json='{"other_config":"must survive"}'::jsonb,version=7"""))
status,body=request(); assert status==200,(status,body)
assert body=={'token_set':False,'token_updated_at':None,'endpoint_path':'/remote/auto/youtube',
'http_method':'POST','authorization_header':'Authorization','idempotency_header':'Idempotency-Key'},body
status,_=request('PUT',{'token':'short'});assert status==400,status
status,body=request('PUT',{'token':'a-demo-token-value-11'});assert status==200 and body['token_set'],(status,body)
assert 'a-demo-token-value-11' not in json.dumps(body)
with engine.connect() as db:
    record=db.execute(text("SELECT value_json,version FROM app_settings WHERE key='remote.api'")).one()
assert record.version==8 and record.value_json['other_config']=='must survive'
assert verify_password_hash('a-demo-token-value-11',record.value_json['token_hash']),(record.value_json['token_hash'][:30],)
assert not verify_password_hash('wrong-token',record.value_json['token_hash'])
assert record.value_json['token_hash'].startswith('pbkdf2_sha256$')
status,body=request('PUT',{});assert status==200 and body['token_set']
with engine.connect() as db:
    assert db.execute(text("SELECT version FROM app_settings WHERE key='remote.api'")).scalar()==8
status,body=request('PUT',{'token':''});assert status==200 and not body['token_set']
with engine.connect() as db:
    record=db.execute(text("SELECT value_json,version FROM app_settings WHERE key='remote.api'")).one()
    assert record.value_json=={'other_config':'must survive'} and record.version==9,record
print('PASS: native original /settings/api read, min token validation, Python PBKDF2 compatibility, no secret echo, preserve unknown fields, revoke')
