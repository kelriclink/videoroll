#!/usr/bin/env python3
"""Isolated Python/Rust V1 publish-batch and cover-upload differential.

Uses the original SQLAlchemy publishing_service + Pydantic response model,
one PostgreSQL throwaway DB, real HTTP multipart images, and V2 asset refs.
"""
import hashlib,io,json,os
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from uuid import UUID,uuid4
from pathlib import Path
from PIL import Image
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from videoroll.apps.orchestrator_api.services import publishing_service
from videoroll.apps.orchestrator_api.schemas import PublishBatchSummary
from videoroll.apps.orchestrator_api.services.image_validation import validate_and_reencode_cover
from fastapi import HTTPException

dburl=os.environ["DATABASE_URL"]
assert dburl.rsplit("/",1)[-1]=="videoroll_test","refuse non-test DB"
engine=create_engine(dburl.replace("postgresql://","postgresql+psycopg://",1))
root="http://127.0.0.1:18080/api/v1"
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
def http(method,path,data=None,ctype="application/json"):
    if isinstance(data,dict):
        data=json.dumps(data).encode()
    req=Request(root+path,data=data,method=method,
        headers={"x-videoroll-v2-internal-token":token,**(
            {"content-type":ctype} if data is not None else {})})
    try:r=urlopen(req,timeout=45)
    except HTTPError as error:r=error
    with r:return r.status,r.headers, r.read()
def api(method,path,data=None):
    status,headers,body=http(method,path,data)
    try:return status,json.loads(body)
    except ValueError:return status,body
def multipart(path,name,payload):
    boundary="rust-cover-differential-boundary"
    body=(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
          f"Content-Type: application/octet-stream\r\n\r\n").encode()+payload+f"\r\n--{boundary}--\r\n".encode()
    status,headers,data=http("POST",path,body,f"multipart/form-data; boundary={boundary}")
    try:return status,json.loads(data)
    except ValueError:return status,data

status,task=api("POST","/tasks",{"source_type":"local","source_license":"own","created_by":"rust-focused-test"})
assert status==201,(status,task)
taskid=task["id"]
task2=api("POST","/tasks",{"source_type":"local","source_license":"own"})[1]["id"]
batch1=str(uuid4())
batch2=str(uuid4())
account=str(uuid4())
old_job=str(uuid4())
new_job=str(uuid4())
different_job=str(uuid4())
with engine.begin() as db:
    db.execute(text("""INSERT INTO publish_batches
        (id,task_id,state,expected_targets,outcomes_json,created_at,updated_at)
        VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'active',
        CAST(:targets AS jsonb),CAST(:outcomes AS jsonb),now()-interval '2 hours',now()-interval '1 hour')"""),
        {"id":batch1,"task":taskid,
         "targets":json.dumps([{"platform":"bilibili"},{"platform":"douyin","account_id":account}]),
         "outcomes":json.dumps({"bilibili:default":{"state":"queued"},"older": {"state":"accepted"}})})
    db.execute(text("""INSERT INTO publish_batches
        (id,task_id,state,expected_targets,outcomes_json,created_at,updated_at)
        VALUES(CAST(:id AS uuid),CAST(:task AS uuid),'finished','[]'::jsonb,
        '{}'::jsonb,now()-interval '15 minutes',now())"""),{"id":batch2,"task":taskid})
    for jid,platform,account_id,state,response,ago in [
        (old_job,"bilibili",None,"failed",{"message":"earlier failure"},120),
        (new_job,"bilibili",None,"unknown",{"message":"gateway uncertain","status_code":504},5),
        (different_job,"douyin",account,"published",None,20),
    ]:
        db.execute(text("""INSERT INTO publish_jobs(
          id,task_id,batch_id,platform,account_id,state,response_json,created_at,updated_at)
          VALUES(CAST(:id AS uuid),CAST(:task AS uuid),CAST(:batch AS uuid),
          :platform,CAST(:account AS uuid),:state,CAST(:response AS jsonb),
          now()-(:age*interval '1 minute'),now()-(:age*interval '1 minute'))"""),
          {"id":jid,"task":taskid,"batch":batch1,"platform":platform,
           "account":account_id,"state":state,
           "response":json.dumps(response) if response is not None else None,"age":ago})
with Session(engine) as db:
    py=[PublishBatchSummary(**item).model_dump(mode="json")
        for item in publishing_service.list_task_publish_batches(UUID(taskid),20,db)]
status,raw=api("GET",f"/tasks/{taskid}/publish_batches")
assert status==200,(status,raw)
assert raw==py,(raw,py)
assert raw[0]["id"]==batch2 and raw[1]["id"]==batch1,raw
assert raw[1]["outcomes"]["bilibili:default"]["state"]=="unknown",raw
assert raw[1]["outcomes"]["bilibili:default"]["detail"]=="gateway uncertain (status_code=504)",raw
assert raw[1]["outcomes"]["older"]["state"]=="accepted"
assert raw[1]["outcomes"][f"douyin:{account}"]["state"]=="published"
status,limited=api("GET",f"/tasks/{taskid}/publish_batches?limit=1")
assert status==200 and len(limited)==1 and limited[0]["id"]==batch2,(status,limited)
assert api("GET",f"/tasks/{task2}/publish_batches")[1]==[]
assert api("GET",f"/tasks/{uuid4()}/publish_batches")[0]==404
assert api("GET",f"/tasks/{taskid}/publish_batches?limit=0")[0] in (400,422)
print("PASS DIFF: original Python/Rust publish_batches, latest job wins, kept prior outcomes, scoped account, ordering and pagination")

for method in ("confirm","rerun"):
    jobid=str(uuid4())
    try:
        from videoroll.apps.orchestrator_api.routers.youdub_native_routes import retired_legacy_workflow
        retired_legacy_workflow(UUID(taskid),UUID(jobid))
        raise AssertionError("original retired endpoint should not succeed")
    except HTTPException as ex:
        assert ex.status_code==410
        expected=ex.detail
    status,body=api("POST",f"/tasks/{taskid}/dubbing_jobs/{jobid}/{method}",{})
    assert status==410 and body.get("detail")==expected,(method,status,body)
print("PASS DIFF: retired YouDub confirm/rerun refuse with original HTTP 410 without creating jobs")

for format_name in ("PNG","JPEG","WEBP"):
    image=Image.new("RGB",(48,32),(24,118,200))
    source=io.BytesIO()
    image.save(source,format=format_name)
    incoming=source.getvalue()
    # A misleading extension must not be trusted; the real pixels/format decide.
    source_name="cover.txt"
    reference=validate_and_reencode_cover(io.BytesIO(incoming))
    assert reference.width==48 and reference.height==32
    status,asset=multipart(f"/tasks/{taskid}/upload/cover",source_name,incoming)
    assert status==200,(format_name,status,asset)
    assert asset["kind"]=="cover_image" and asset["task_id"]==taskid,asset
    assert asset["storage_key"].endswith(reference.extension),(format_name,asset,reference)
    status,headers,download=http("GET",f"/tasks/{taskid}/assets/{asset['id']}/download")
    assert status==200,(format_name,status)
    with Image.open(io.BytesIO(download)) as view:
        view.load()
        assert view.size==(48,32) and view.format==format_name,(format_name,view.format,view.size)
    assert headers.get("Content-Type")==reference.content_type,(format_name,headers.get("Content-Type"))
    assert len(download)==asset["size_bytes"]
    assert hashlib.sha256(download).hexdigest()==asset["sha256"]
    with engine.connect() as db:
        row=db.execute(text("""SELECT
          (SELECT count(*) FROM storage_objects WHERE storage_key=:key AND state='live') AS objects,
          (SELECT count(*) FROM storage_object_refs WHERE storage_key=:key
           AND owner_type='task' AND owner_id=CAST(:task AS uuid) AND role='cover_image') AS refs"""),
          {"key":asset["storage_key"],"task":taskid}).one()
    assert row==(1,1),(row,format_name)
    print(f"PASS: original cover contract {format_name}, validated pixels, re-encoded asset, content type, SHA256, durable reference")
status,reject=multipart(f"/tasks/{taskid}/upload/cover","not-image.png",b"<html>not a real image</html>")
assert status==400,(status,reject)
status,reject=multipart(f"/tasks/{taskid}/upload/cover","empty.jpg",b"")
assert status==400,(status,reject)
status,reject=multipart(f"/tasks/{uuid4()}/upload/cover","photo.png",incoming)
assert status==404,(status,reject)
storage=Path(os.environ["VIDEOROLL_V2_STORAGE_ROOT"])/"v2"/"artifacts"/taskid
assert storage.exists()
orphans=[path.name for path in storage.iterdir()
        if path.name.endswith((".cover-uploading",".canonical.png",".canonical.jpg",".canonical.webp"))]
assert not orphans,orphans
print("PASS: invalid cover and missing V2 task fail without orphaned staging files")
