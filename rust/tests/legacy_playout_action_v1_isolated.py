"""Original POST /tasks/{id}/assets/{id}/playout contract on unified table and filesystem."""
import hashlib,json,os,uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from sqlalchemy import create_engine,text
uri=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
assert '127.0.0.1:' in uri and uri.endswith('/videoroll_test')
engine=create_engine(uri)
task=str(uuid.uuid4());asset=str(uuid.uuid4());media=b'VERIFIED TEST ASSET: FFMPEG VALIDITY NOT REQUIRED FOR FFPLAYOUT IMPORT\n'*800
key=f'v2/raw/{task}/legacy-playout-test.mp4'
root=Path(os.environ['VIDEOROLL_V2_STORAGE_ROOT']);src=root/key
src.parent.mkdir(parents=True,exist_ok=True);src.write_bytes(media)
with engine.begin() as db:
    db.execute(text("""INSERT INTO tasks(id,source_type,source_license,status,runtime_owner)
      VALUES(CAST(:id AS uuid),'local','own','rendered','rust')"""),{'id':task})
    db.execute(text("""INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes)
      VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'video_final',:key,:sha,:sz)"""),
      {'id':asset,'task':task,'key':key,'sha':hashlib.sha256(media).hexdigest(),'sz':len(media)})
def post(t=task,a=asset):
    path=f'http://127.0.0.1:18080/api/v1/tasks/{t}/assets/{a}/playout'
    r=Request(path,data=b'{}',method='POST',headers={'x-videoroll-v2-internal-token':os.environ['VIDEOROLL_V2_INTERNAL_TOKEN'],'content-type':'application/json'})
    try:resp=urlopen(r,timeout=16)
    except HTTPError as e:resp=e
    with resp:
        raw=resp.read()
        try:body=json.loads(raw)
        except ValueError:body=raw.decode(errors='replace')
        return resp.status,body
status,first=post();assert status==200,(status,first)
assert first['status']=='ready' and first['asset_id']==asset and first['task_id']==task
assert first['playout_path']==first['relative_media_path']
assert first['ffplayout_channel_id']==1 and first['transfer_mode'] in ('copy','hardlink')
assert first['source_checksum']==hashlib.sha256(media).hexdigest()
assert set(first)=={'id','task_id','asset_id','status','ffplayout_channel_id','playout_path','relative_media_path','transfer_mode','source_checksum','created_at','updated_at'}
status,again=post();assert status==200 and again['id']==first['id'],(status,again)
with engine.connect() as db:
    assert db.execute(text("SELECT count(*) FROM playout_asset_links WHERE task_id=CAST(:task AS uuid)"),{'task':task}).scalar()==1
    assert db.execute(text("SELECT count(*) FROM storage_object_refs WHERE owner_type='playout' AND owner_id=CAST(:id AS uuid)"),{'id':first['id']}).scalar()==1
status,_=post(t=str(uuid.uuid4()));assert status==404,status
print('PASS: original V1 add ffplayout maps native importer to exact DTO; repeat uses one durable link and one GC ref; cross-task denied')
