#!/usr/bin/env python3
"""Python source/Rust HTTP differential: disposable PostgreSQL and loopback only."""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from videoroll.config import SubtitleServiceSettings
from videoroll.apps.subtitle_service.embeddings import list_local_embedding_models,delete_local_embedding_model
from videoroll.apps.subtitle_service.main import get_embedding_runtime_status

url=os.environ["DATABASE_URL"]
assert url.rsplit("/",1)[-1]=="videoroll_test", "non-test database forbidden"
# The Python settings model requires REDIS_URL even for this DB-only read.
# The loopback placeholder is never contacted by this diagnostic.
os.environ.setdefault("REDIS_URL","redis://127.0.0.1:6379/15")
engine=create_engine(url.replace("postgresql://","postgresql+psycopg://",1))
root=Path(__file__).resolve().parents[1]/"target/v2-smoke-embedding-models"
root.mkdir(parents=True,exist_ok=True)
for name,size in (("b-model",5),("A-model",7),("py-delete",4),("rust-delete",4)):
    d=root/name
    d.mkdir(exist_ok=True)
    (d/"model.bin").write_bytes(b"x"*size)
(root/"b-model"/"nested").mkdir(exist_ok=True)
(root/"b-model"/"nested"/"weights.bin").write_bytes(b"data")
alt=root/"other-root"
(alt/"z-model").mkdir(parents=True,exist_ok=True)
(alt/"z-model"/"config.json").write_text('{"version":1}')
with engine.begin() as c:
    raw=c.execute(text("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")).scalar() or {}
    raw=dict(raw)
    raw.update({"rag_embedding_model_dir":str(root),"rag_embedding_model":"text-embedding-3-small",
        "rag_embedding_provider":"openai","rag_embedding_dimensions":3,"rag_embedding_device":"cpu",
        "rag_embedding_openai":{}})
    c.execute(text("""INSERT INTO app_settings(key,value_json,version)
        VALUES('subtitle.translate',CAST(:value AS jsonb),1)
        ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"""),
        {"value":json.dumps(raw)})
def call(method,path,body=None):
    req=Request("http://127.0.0.1:18080/api/v1"+path,method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"],
                 "content-type":"application/json"})
    try: response=urlopen(req,timeout=15)
    except HTTPError as e: response=e
    with response:
        return response.status,json.loads(response.read())
assert call("GET","/subtitle/embedding/models")==(
    200,list_local_embedding_models(root))
assert call("POST","/subtitle/embedding/models/list",{"model_dir":str(alt)})==(
    200,list_local_embedding_models(alt))
assert call("POST","/subtitle/embedding/models/list",{})==(
    200,list_local_embedding_models(root))
delete_local_embedding_model(model_dir=root,name="py-delete")
assert not (root/"py-delete").exists()
assert call("DELETE","/subtitle/embedding/models/rust-delete")==(
    200,{"deleted":True})
assert not (root/"rust-delete").exists()
assert call("DELETE","/subtitle/embedding/models/absent")==(
    200,{"deleted":True})
outside=root.parent/"embedding-symlink-target"
outside.mkdir(exist_ok=True)
link=root/"outside-link"
if not link.is_symlink():link.symlink_to(outside,target_is_directory=True)
assert call("DELETE","/subtitle/embedding/models/outside-link")[0]==400
assert outside.exists()
# Exercise the actual native HF snapshot fetch from the isolated loopback hub.
status,download=call("POST","/subtitle/embedding/models/download",{
    "model":"BAAI/bge-small-zh-v1.5","name":"bge-mock"})
assert status==200,(status,download)
assert download["name"]=="bge-mock" and (root/"bge-mock"/"weights/embedding.bin").read_bytes()==b"offline-embedding-fixture"
assert call("POST","/subtitle/embedding/models/download",{
    "model":"BAAI/bge-small-zh-v1.5","name":"bge-mock"})[0] in (400,409,500,502)
status,replaced=call("POST","/subtitle/embedding/models/download",{
    "model":"BAAI/bge-small-zh-v1.5","name":"bge-mock","force":True})
assert status==200,(status,replaced)

with engine.begin() as c:
    c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    c.execute(text("""CREATE TABLE IF NOT EXISTS translation_knowledge_items(
        id uuid PRIMARY KEY,embedding vector,embedding_model text NOT NULL DEFAULT '')"""))
    c.execute(text("""INSERT INTO translation_knowledge_items(id,embedding,embedding_model)
      VALUES ('00000000-0000-0000-0000-000000010311','[1,0,0]'::vector,'text-embedding-3-small'),
             ('00000000-0000-0000-0000-000000010312','[0,1,0]'::vector,'older')
      ON CONFLICT(id) DO NOTHING"""))
with Session(engine) as s:
    original=get_embedding_runtime_status(settings=SubtitleServiceSettings(),db=s)
    expected=original.model_dump(mode="json")
status,actual=call("GET","/subtitle/embedding/runtime")
assert status==200,(status,actual)
for key,value in expected.items():
    assert actual[key]==value,(key,value,actual[key])
assert actual["total_embeddings"]>=2 and actual["active_embeddings"]>=1,actual
print("PASS: Embedding model inventory, directory override, real deletion, path safety, PostgreSQL pgvector runtime Python/Rust differential")
