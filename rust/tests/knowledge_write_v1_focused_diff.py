#!/usr/bin/env python3
"""Disposable Python/Rust Knowledge upsert and Embedding rebuild differential.

Runs the real original Python handler, a loopback HTTP Embedding server,
the native Rust HTTP route, and checks original PostgreSQL rows byte-for-byte.
"""
from __future__ import annotations
import json,os,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from uuid import UUID
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from videoroll.apps.subtitle_service import main as legacy
from videoroll.apps.subtitle_service.schemas import KnowledgeItemUpsertRequest,KnowledgeEmbeddingRebuildRequest
from videoroll.apps.subtitle_service.rag import rebuild_knowledge_embeddings,rag_settings_from_translate_settings
from videoroll.apps.subtitle_service.embeddings import embedding_settings_from_translate_settings
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.config import SubtitleServiceSettings
from videoroll.apps.subtitle_service.embeddings import embed_text
from videoroll.apps.subtitle_service.rag import build_knowledge_embedding_text
from videoroll.apps.subtitle_service.schemas import DictionaryPromoteRequest
os.environ.setdefault("REDIS_URL","redis://127.0.0.1:6379/15")

url=os.environ["DATABASE_URL"]
assert url.endswith("/videoroll_test"),"refuse non-test DB"
engine=create_engine(url.replace("postgresql://","postgresql+psycopg://",1))
# AI usage accounting opens its own SQLAlchemy pool; force psycopg3 in this
# isolated test environment (the host venv has no psycopg2 driver).
os.environ["DATABASE_URL"]=url.replace("postgresql://","postgresql+psycopg://",1)
server_base="http://127.0.0.1:18095/v1"
calls=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_args):pass
    def do_POST(self):
        assert self.path=="/v1/embeddings",self.path
        assert self.headers["Authorization"]=="Bearer loopback-embedding-token"
        count=int(self.headers.get("Content-Length","0"))
        body=json.loads(self.rfile.read(count))
        assert body["model"]=="mock-embed-3"
        assert body["dimensions"]==3
        assert isinstance(body["input"],str) and body["input"].startswith("type:")
        calls.append(body)
        raw=[0.5,-0.25,0.125] if "bad-dimension" not in body["input"] else [0.5]
        data=json.dumps({"data":[{"embedding":raw,"index":0}],"model":"mock-embed-3"}).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)
server=ThreadingHTTPServer(("127.0.0.1",18095),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True)
thread.start()
settings=SubtitleServiceSettings()
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
root="http://127.0.0.1:18080/api/v1"
def call(path,body):
    req=Request(root+path,method="POST",data=json.dumps(body,ensure_ascii=False).encode(),
        headers={"x-videoroll-v2-internal-token":token,"Content-Type":"application/json"})
    try:response=urlopen(req,timeout=30)
    except HTTPError as exc:response=exc
    with response:return response.status,json.loads(response.read())
def rows(term,domain):
    with engine.connect() as db:
        data=db.execute(text("""SELECT id,item_type,term,normalized_term,translation,target_lang,
         domain,aliases,title,content,description,sources,confidence,status,created_by,
         embedding::text AS embedding,embedding_model,embedding_text_hash
         FROM translation_knowledge_items WHERE term=:term AND domain=:domain
         ORDER BY created_at DESC LIMIT 1"""),{"term":term,"domain":domain}).mappings().first()
        return dict(data) if data else None
def comparable(row):
    return {k:v for k,v in row.items() if k!="id"} if row else None
raw_config={
 "rag_embedding_provider":"openai",
 "rag_embedding_model":"mock-embed-3",
 "rag_embedding_dimensions":3,
 "rag_embedding_openai":{"api_key_enc":"dbplain:v1:loopback-embedding-token",
                         "base_url":server_base,"timeout_seconds":10.0}
}
with engine.begin() as db:
    old=db.execute(text("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")).scalar()
    db.execute(text("""INSERT INTO app_settings(key,value_json,updated_at)
         VALUES('subtitle.translate',CAST(:cfg AS jsonb),now())
         ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,updated_at=now()"""),
         {"cfg":json.dumps(raw_config)})
try:
    def reference(payload):
        with Session(engine) as db:
            return str(legacy.upsert_knowledge_item_view(
                payload=KnowledgeItemUpsertRequest(**payload),settings=settings,db=db).id)
    # Term insert -> persist vector; match Python's original row.
    base={"item_type":"term","target_lang":"zh","term":"RUST_DIFF_PCB / trace_Width",
          "translation":"走线宽度","domain":"test-isolated","aliases":["trace","Trace"],
          "confidence":0.92,"status":"approved","created_by":"manual",
          "sources":[{"source":"fixture","url":""}]}
    with Session(engine) as db:
        cfg=get_translate_settings(db,settings)
        assert cfg["rag_embedding_model"]=="mock-embed-3",cfg["rag_embedding_model"]
        assert cfg["rag_embedding_api_key_set"]
        text_probe=build_knowledge_embedding_text(
            item_type=base["item_type"],term=base["term"],
            translation=base["translation"],domain=base["domain"],
            aliases=base["aliases"])
        try:
            py_probe=embed_text(text_probe,settings=embedding_settings_from_translate_settings(cfg))
        except Exception as exc:
            raise AssertionError("Original Python embedding HTTP failed against isolated fixture: "+str(exc)) from exc
        assert py_probe==[0.5,-0.25,0.125],py_probe
    py_id=reference(base)
    py=rows(base["term"],base["domain"])
    with engine.begin() as db:
        db.execute(text("DELETE FROM translation_knowledge_items WHERE id=CAST(:id AS uuid)"),
                   {"id":py_id})
    status,result=call("/subtitle/knowledge/items",base)
    assert status==200,(status,result)
    rust_id=result["id"]
    rust=rows(base["term"],base["domain"])
    assert comparable(rust)==comparable(py),(rust,py)
    assert rust["embedding"]=="[0.5,-0.25,0.125]",rust
    assert rust["embedding_model"]=="openai:mock-embed-3"
    # Dedupe is scoped by target_lang + domain, not global.
    changed=dict(base,translation="布线宽度",status="pending")
    status,result=call("/subtitle/knowledge/items",changed)
    assert status==200 and result["id"]==rust_id,(status,result)
    assert rows(base["term"],base["domain"])["translation"]=="布线宽度"
    other=dict(base,domain="different-isolated-domain")
    status,result=call("/subtitle/knowledge/items",other)
    assert status==200 and result["id"]!=rust_id,(status,result)
    # Document requires real vector. Test Python and Rust normal outcome.
    doc={"item_type":"document","title":"Rust testing notes","content":"M3 technical document",
         "domain":"test-docs","status":"approved"}
    # Python creates same empty term/domain combo; compare by saved id.
    doc_py=reference(doc)
    with engine.connect() as db:
        expected=db.execute(text("""SELECT embedding::text,embedding_model,embedding_text_hash
          FROM translation_knowledge_items WHERE id=CAST(:id AS uuid)"""),
          {"id":doc_py}).one()
    status,doc_result=call("/subtitle/knowledge/items",doc)
    assert status==200,(status,doc_result)
    with engine.connect() as db:
        actual=db.execute(text("""SELECT embedding::text,embedding_model,embedding_text_hash
          FROM translation_knowledge_items WHERE id=CAST(:id AS uuid)"""),
          {"id":doc_result["id"]}).one()
    assert expected==actual,(expected,actual)
    bad={"item_type":"document","title":"bad-dimension","content":"invalid"}
    assert call("/subtitle/knowledge/items",bad)[0]==502
    assert call("/subtitle/knowledge/items",{"item_type":"term","term":"","translation":"X"})[0]==400
    assert call("/subtitle/knowledge/items",{"item_type":"term","term":"Term","translation":""})[0]==400
    # Rebuild on two term rows: original Python side effects vs Rust side effects.
    with engine.begin() as db:
        db.execute(text("""UPDATE translation_knowledge_items SET status='rust-embedding-regression',
          embedding=NULL,embedding_model='',embedding_text_hash='',last_verified_at=NULL
          WHERE id IN(CAST(:a AS uuid),CAST(:b AS uuid))"""),
          {"a":rust_id,"b":result["id"]})
    with Session(engine) as db:
        config=get_translate_settings(db,settings)
        result_py=rebuild_knowledge_embeddings(db,
            rag_settings=rag_settings_from_translate_settings(config),
            embedding_settings=embedding_settings_from_translate_settings(config),
            item_type="term",status="rust-embedding-regression",limit=2)
        db.commit()
    with engine.connect() as db:
        expected=db.execute(text("""SELECT id,embedding::text,embedding_model,embedding_text_hash
           FROM translation_knowledge_items WHERE status='rust-embedding-regression'
           ORDER BY id""")).all()
    with engine.begin() as db:
        db.execute(text("""UPDATE translation_knowledge_items
           SET embedding=NULL,embedding_model='',embedding_text_hash='',last_verified_at=NULL
           WHERE status='rust-embedding-regression'"""))
    status,rebuild=call("/subtitle/knowledge/rebuild-embeddings",
        {"item_type":"term","status":"rust-embedding-regression","limit":2})
    assert status==200,(status,rebuild)
    for field in ("total","updated","failed","skipped","embedding_model","dimensions"):
        assert rebuild[field]==result_py[field],(field,rebuild,result_py)
    with engine.connect() as db:
        actual=db.execute(text("""SELECT id,embedding::text,embedding_model,embedding_text_hash
           FROM translation_knowledge_items WHERE status='rust-embedding-regression'
           ORDER BY id""")).all()
    assert actual==expected,(actual,expected)
    # The existing dictionary promotion endpoint now shares the same live
    # OpenAI Embedding transport, not only the newly written knowledge API.
    dictionary_id="00000000-0000-0000-0000-000000093451"
    entry_id="00000000-0000-0000-0000-000000093452"
    with engine.begin() as db:
        db.execute(text("""INSERT INTO translation_dictionary_sources
          (id,name,slug,source_lang,target_lang,license,source_url,attribution)
          VALUES(CAST(:id AS uuid),'V1 Vector Glossary','v1-vector-glossary',
                 'en','zh','MIT','https://example.test/terms','Vector evidence')"""),
            {"id":dictionary_id})
        db.execute(text("""INSERT INTO translation_dictionary_entries
          (id,source_id,source_lang,target_lang,term,normalized_term,
           translations,translation_text,domain,definition,aliases,quality)
          VALUES(CAST(:id AS uuid),CAST(:source_id AS uuid),'en','zh',
           'V1 Vector-Promotion','v1 vector promotion','["向量提升"]'::jsonb,
           '向量提升','embedding-comparison','technical description',
           '["extra"]'::jsonb,0.92)"""),
            {"id":entry_id,"source_id":dictionary_id})
    with Session(engine) as db:
        promoted=legacy.promote_dictionary_entry_view(
            payload=DictionaryPromoteRequest(entry_id=UUID(entry_id)),
            settings=settings,db=db)
    original_promoted=rows("V1 Vector-Promotion","embedding-comparison")
    assert original_promoted["embedding"],original_promoted
    with engine.begin() as db:
        db.execute(text("DELETE FROM translation_knowledge_items WHERE id=CAST(:id AS uuid)"),
            {"id":str(promoted.knowledge_item_id)})
    status,result_promoted=call("/subtitle/dictionaries/promote",{"entry_id":entry_id})
    assert status==200,(status,result_promoted)
    native_promoted=rows("V1 Vector-Promotion","embedding-comparison")
    assert comparable(native_promoted)==comparable(original_promoted),(
        original_promoted,native_promoted)
    # Real multipart upload through the Rust API, with the same live mocked
    # Embedding transport and original PostgreSQL business table as Python.
    def import_multipart(filename, content, import_format="auto"):
        boundary="----videoroll-bulk-knowledge-isolated"
        data=bytearray()
        for name,value,part_filename in (
            ("import_format",import_format,None),
            ("target_lang","zh",None),
            ("domain","bulk-fixture",None),
            ("file",content,filename),
        ):
            data.extend(f"--{boundary}\r\n".encode())
            header=f'Content-Disposition: form-data; name="{name}"'
            if part_filename is not None:header+=f'; filename="{part_filename}"'
            data.extend((header+"\r\n\r\n").encode())
            data.extend(value if isinstance(value,bytes) else value.encode())
            data.extend(b"\r\n")
        data.extend(f"--{boundary}--\r\n".encode())
        req=Request(root+"/subtitle/knowledge/import",method="POST",data=bytes(data),
            headers={"x-videoroll-v2-internal-token":token,
                     "Content-Type":f"multipart/form-data; boundary={boundary}"})
        try:response=urlopen(req,timeout=40)
        except HTTPError as exc:response=exc
        with response:return response.status,json.loads(response.read())
    import_json=json.dumps({"items":[
        {"term":"RUST_IMPORT_JSON","translation":"知识导入"},
        {"term":"RUST_IMPORT_SKIPPED","translation":""}
    ]},ensure_ascii=False).encode()
    status,imported=import_multipart("knowledge.json",import_json)
    assert status==200 and imported["format"]=="json" and imported["parsed"]==2,(status,imported)
    assert (imported["imported"],imported["skipped"],imported["failed"])==(1,1,0),imported
    original_id=imported["ids"][0]
    status,again=import_multipart("knowledge.json",import_json)
    assert status==200 and again["ids"]==[original_id],(status,again)
    saved=rows("RUST_IMPORT_JSON","bulk-fixture")
    assert saved and saved["embedding"] and saved["id"]==UUID(original_id),saved
    status,csv_out=import_multipart("knowledge.csv",b"term,translation,aliases\nRUST_IMPORT_CSV,CSV translation,alias1|alias2\n")
    assert status==200 and csv_out["format"]=="csv" and csv_out["imported"]==1,(status,csv_out)
    assert rows("RUST_IMPORT_CSV","bulk-fixture")["aliases"]==["alias1","alias2"]
    srt=b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:02,000 --> 00:00:03,000\nworld\n"
    status,srt_out=import_multipart("lesson.srt",srt)
    assert status==200 and srt_out["format"]=="srt" and srt_out["imported"]==1,(status,srt_out)
    with engine.connect() as db:
        doc=db.execute(text("""SELECT content,embedding::text,created_by FROM translation_knowledge_items
           WHERE id=CAST(:id AS uuid)"""),{"id":srt_out["ids"][0]}).one()
    assert doc[0]=="Hello\nworld" and doc[1] and doc[2]=="bulk:srt",doc
    status,bad_format=import_multipart("invalid.xml",b"<xml />")
    assert status==400,(status,bad_format)
    print("PASS DIFF: native multipart JSON/CSV/SRT import, dedupe, skip and live Embedding persisted in original table")
    print("PASS DIFF: original dictionary promotion now stores the same online Embedding vector, hash and metadata")
    print(f"PASS DIFF: Rust V1 knowledge create/dedupe/doc vector and rebuilding with real loopback OpenAI-compatible embedding ({len(calls)} network requests)")
finally:
    server.shutdown()
    server.server_close()
    with engine.begin() as db:
        if old is None:
            db.execute(text("DELETE FROM app_settings WHERE key='subtitle.translate'"))
        else:
            db.execute(text("UPDATE app_settings SET value_json=CAST(:v AS jsonb) WHERE key='subtitle.translate'"),
                {"v":json.dumps(old)})
