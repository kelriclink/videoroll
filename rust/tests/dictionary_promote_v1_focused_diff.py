#!/usr/bin/env python3
"""Offline real-DB differential for Python dictionary promotion vs native Rust.

Uses a single seeded source/entry, resets the knowledge row between reference
and Rust writes, and checks storage data rather than only HTTP status.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from uuid import UUID
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from videoroll.config import SubtitleServiceSettings
from videoroll.apps.subtitle_service.schemas import DictionaryPromoteRequest
from videoroll.apps.subtitle_service import main as python

url=os.environ["DATABASE_URL"]
assert url.rsplit("/",1)[-1]=="videoroll_test", "refuse non-test database"
os.environ.setdefault("REDIS_URL","redis://127.0.0.1:6379/15")
engine=create_engine(url.replace("postgresql://","postgresql+psycopg://",1))
source="00000000-0000-0000-0000-000000010381"
entry="00000000-0000-0000-0000-000000010382"
no_translation="00000000-0000-0000-0000-000000010383"
other_domain="00000000-0000-0000-0000-000000010384"
with engine.begin() as c:
    c.execute(text("""INSERT INTO translation_dictionary_sources
      (id,name,slug,source_lang,target_lang,format,license,source_url,attribution,entry_count)
      VALUES (CAST(:id AS uuid),'Regression Glossary','rust-promote-test','en','zh',
          'csv','MIT','https://example.test/terms','Maintained by test',3)
      ON CONFLICT(id) DO NOTHING"""),{"id":source})
    c.execute(text("""INSERT INTO translation_dictionary_entries
       (id,source_id,source_lang,target_lang,term,normalized_term,translations,
        translation_text,domain,definition,aliases,quality)
       VALUES(CAST(:id AS uuid),CAST(:source_id AS uuid),'en','zh',
        'PCB/Trace_Width','pcb trace width',CAST(:translations AS jsonb),
        :translation,:domain,'Width of the copper trace',
        CAST(:aliases AS jsonb),0.92)
       ON CONFLICT(id) DO NOTHING"""),{"id":entry,"source_id":source,
          "translations":json.dumps(["走线宽度","布线宽度"],ensure_ascii=False),
          "translation":"走线宽度","domain":"pcb",
          "aliases":json.dumps(["Trace Width","走线"],ensure_ascii=False)})
    c.execute(text("""INSERT INTO translation_dictionary_entries
       (id,source_id,source_lang,target_lang,term,normalized_term,translations,domain)
       VALUES(CAST(:id AS uuid),CAST(:source_id AS uuid),'en','zh',
        'Empty term','empty term','[]'::jsonb,'pcb')
       ON CONFLICT(id) DO NOTHING"""),{"id":no_translation,"source_id":source})
    c.execute(text("""INSERT INTO translation_dictionary_entries
       (id,source_id,source_lang,target_lang,term,normalized_term,translations,
        translation_text,domain,quality)
       VALUES(CAST(:id AS uuid),CAST(:source_id AS uuid),'en','zh',
        'PCB/Trace_Width','pcb trace width','["不同领域术语"]'::jsonb,
        '不同领域术语','mechanical',0.9)
       ON CONFLICT(id) DO NOTHING"""),{"id":other_domain,"source_id":source})
def call(method,path,body=None):
    req=Request("http://127.0.0.1:18080/api/v1"+path,
       data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,method=method,
       headers={"x-videoroll-v2-internal-token":os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"],
                "content-type":"application/json"})
    try: res=urlopen(req,timeout=15)
    except HTTPError as e: res=e
    with res: return res.status,json.loads(res.read())
def knowledge(item_id):
    with engine.connect() as c:
        row=c.execute(text("""SELECT item_type,term,normalized_term,translation,
          target_lang,domain,aliases,title,content,description,sources,confidence,
          status,created_by,embedding_model,embedding_text_hash
          FROM translation_knowledge_items WHERE id=CAST(:id AS uuid)"""),
          {"id":str(item_id)}).mappings().first()
        assert row is not None
        return dict(row)
# Python embedding is explicitly disabled to isolate the common nonvector
# promotion behavior. The native handler rejects configured embedding paths
# rather than silently omitting a generated vector.
settings=SubtitleServiceSettings()
with Session(engine) as s, patch.object(python,"embed_text",side_effect=RuntimeError("offline fixture")):
    result=python.promote_dictionary_entry_view(
        payload=DictionaryPromoteRequest(entry_id=UUID(entry)),settings=settings,db=s)
    expected=knowledge(result.knowledge_item_id)
with engine.begin() as c:
    c.execute(text("DELETE FROM translation_knowledge_items WHERE id=CAST(:id AS uuid)"),
              {"id":str(result.knowledge_item_id)})
status,body=call("POST","/subtitle/dictionaries/promote",{"entry_id":entry})
assert status==200,(status,body)
item_id=body["knowledge_item_id"]
actual=knowledge(item_id)
assert actual==expected,(expected,actual)
assert actual["normalized_term"]=="pcb/trace width",actual
assert actual["confidence"]==0.92,actual
status,again=call("POST","/subtitle/dictionaries/promote",
                  {"entry_id":entry,"status":"rejected","confidence":0.4})
assert status==200 and again["knowledge_item_id"]==item_id,(status,again)
assert knowledge(item_id)["status"]=="rejected"
status,separate=call("POST","/subtitle/dictionaries/promote",{"entry_id":other_domain})
assert status==200 and separate["knowledge_item_id"]!=item_id,(status,separate)
assert knowledge(separate["knowledge_item_id"])["domain"]=="mechanical"
assert call("POST","/subtitle/dictionaries/promote",{"entry_id":no_translation})[0]==400
assert call("POST","/subtitle/dictionaries/promote",
            {"entry_id":"00000000-0000-0000-0000-000000010399"})[0]==404
assert call("POST","/subtitle/dictionaries/promote",
            {"entry_id":entry,"confidence":1.5})[0] in (400,422)
# Original Python knowledge-list fields and the native V1 HTTP projection.
from videoroll.apps.subtitle_service.rag import list_knowledge_items
from videoroll.apps.subtitle_service.schemas import KnowledgeItemRead
with Session(engine) as db:
    original=[KnowledgeItemRead(**row).model_dump(mode="json")
        for row in list_knowledge_items(db,q="PCB/Trace")]
status,actual=call("GET","/subtitle/knowledge/items?q=PCB%2FTrace")
assert status==200,(status,actual)
ignore={"created_at","updated_at","last_verified_at"}
norm=lambda rows:[{k:v for k,v in row.items() if k not in ignore} for row in rows]
assert norm(actual)==norm(original),(norm(actual),norm(original))
from videoroll.apps.subtitle_service.rag import delete_knowledge_item
with Session(engine) as db:
    assert delete_knowledge_item(db,item_id)
    db.commit()
target=separate["knowledge_item_id"]
assert call("DELETE",f"/subtitle/knowledge/items/{target}")==(
    200,{"deleted":True})
assert call("DELETE",f"/subtitle/knowledge/items/{target}")[0]==404
with engine.connect() as con:
    remaining=con.execute(text("SELECT COUNT(*) FROM translation_knowledge_items WHERE id IN (CAST(:a AS uuid),CAST(:b AS uuid))"),
                          {"a":item_id,"b":target}).scalar()
    assert remaining==0,remaining
print("PASS DIFF: original Python/Rust knowledge record deletion and 404")
print("PASS DIFF: original Python/Rust knowledge GET filters and original-table DTO fields")
print("PASS: Python/Rust dictionary to RAG promotion transactional row equivalence, scoped deduplication, status update and errors")
