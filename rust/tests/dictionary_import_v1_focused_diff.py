#!/usr/bin/env python3
"""Disposable same-schema Python/Rust glossary file-import differential.

Probes actual multipart, checksum, archive, batch audit, upsert/replace and
normalized entry fields on isolated PostgreSQL only. Never targets production.
"""
from __future__ import annotations
import hashlib, json, os, uuid
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from videoroll.apps.subtitle_service.dictionaries import import_dictionary_file, lookup_dictionary_entries

url=os.environ["DATABASE_URL"]
assert url.rsplit("/",1)[-1]=="videoroll_test","refuse non-disposable database"
base="http://127.0.0.1:18080/api/v1"
token=os.environ["VIDEOROLL_V2_INTERNAL_TOKEN"]
engine=create_engine(url.replace("postgresql://","postgresql+psycopg://",1))
fixtures=Path(__file__).resolve().parents[1]/"target/v2-smoke-dictionary-fixtures"
fixtures.mkdir(parents=True,exist_ok=True)
def call(file_data:bytes,filename:str,fields:dict[str,str]):
    boundary="v2-test-"+uuid.uuid4().hex
    data=bytearray()
    for k,v in fields.items():
        data.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    data.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    data.extend(file_data)
    data.extend(f"\r\n--{boundary}--\r\n".encode())
    request=Request(base+"/subtitle/dictionaries/import",method="POST",data=bytes(data),
        headers={"x-videoroll-v2-internal-token":token,
                 "content-type":f"multipart/form-data; boundary={boundary}"})
    try: result=urlopen(request,timeout=35)
    except HTTPError as err:result=err
    with result:
        return result.status,json.loads(result.read())
def snapshot(slug:str):
    with engine.connect() as con:
        source=con.execute(text("""SELECT id,name,slug,description,source_lang,
             target_lang,format,license,license_url,source_url,version,attribution,
             domain,priority,enabled,metadata,entry_count
             FROM translation_dictionary_sources WHERE slug=:slug"""),{"slug":slug}).mappings().first()
        assert source,(slug,"missing source")
        rows=con.execute(text("""SELECT source_lang,target_lang,term,normalized_term,
            translations,translation_text,pos,definition,domain,tags,aliases,
            examples,metadata,quality,enabled
            FROM translation_dictionary_entries WHERE source_id=:id
            ORDER BY normalized_term,domain"""),{"id":source["id"]}).mappings().all()
        batch=con.execute(text("""SELECT status,filename,file_sha256,file_size_bytes,
           format,import_mode,requested_by,stats,archive_path
           FROM translation_dictionary_import_batches WHERE source_id=:id
           ORDER BY created_at DESC LIMIT 1"""),{"id":source["id"]}).mappings().first()
        assert batch
        return dict(source),[dict(r) for r in rows],dict(batch)
def normalized_source(x):
    return {k:v for k,v in x.items() if k not in ("id","slug","name")}
def check_case(case, filename,content,format_name, source_lang="",target_lang="zh",max_entries=250000,
               mode="upsert",preset=False):
    py_slug="py-gloss-"+case
    rs_slug="rs-gloss-"+case
    path=fixtures/filename
    path.write_bytes(content)
    details={"source_lang":source_lang,"target_lang":target_lang,
        "format_name":format_name,"description":"dictionary integration",
        "license":"MIT","license_url":"https://example.test/license",
        "source_url":"https://example.test/source","version":"1.0","domain":"electronics",
        "priority":"30","enabled":"true","import_mode":mode,
        "max_entries":str(max_entries)}
    if preset:
        details.update({"dictionary_preset":"ecdict","priority":"0","format_name":"auto",
                        "description":"","license":"","license_url":"","source_url":""})
    py_cfg=dict(details)
    if preset:
        py_cfg.update({"format_name":"ecdict","description":"Free English to Chinese dictionary database.",
            "license":"MIT","license_url":"https://github.com/skywind3000/ECDICT/blob/master/LICENSE",
            "source_url":"https://github.com/skywind3000/ECDICT","priority":"80",
            "source_lang":"en"})
    try:
        python_kwargs={
            "path":path,"filename":filename,"archive_path":str(path),
            "source_name":f"Compare {case}","slug":py_slug,
            "description":py_cfg["description"],"source_lang":py_cfg["source_lang"],
            "target_lang":target_lang,"format_name":py_cfg["format_name"],"license":py_cfg["license"],
            "license_url":py_cfg["license_url"],"source_url":py_cfg["source_url"],
            "version":"1.0","attribution":"","domain":"electronics",
            "priority":int(py_cfg["priority"]),"enabled":True,"import_mode":mode,
            "max_entries":0 if preset and max_entries==250000 else max_entries}
        with Session(engine) as db:
            reference=import_dictionary_file(db,**python_kwargs)
            db.commit()
    except Exception as err:
        raise AssertionError((case,"Python failed",err)) from err
    fields=dict(details,name=f"Compare {case}",slug=rs_slug)
    status,rust=call(content,filename,fields)
    assert status==200,(case,status,rust)
    assert rust["status"]=="succeeded"
    for key in ("parsed","upserted","skipped","max_entries","full_import","sha256"):
        assert rust[key]==reference[key],(case,key,rust[key],reference[key])
    assert rust["sha256"]==hashlib.sha256(content).hexdigest()
    source_py,entries_py,batch_py=snapshot(py_slug)
    source_rs,entries_rs,batch_rs=snapshot(rs_slug)
    assert normalized_source(source_py)==normalized_source(source_rs),(
        case,"source fields",normalized_source(source_py),normalized_source(source_rs))
    assert entries_py==entries_rs,(case,"entries",entries_py,entries_rs)
    for key in ("status","filename","file_sha256","file_size_bytes","format",
                "import_mode","requested_by","stats"):
        assert batch_py[key]==batch_rs[key],(case,"batch",key,batch_py[key],batch_rs[key])
    path_saved=Path(batch_rs["archive_path"])
    assert path_saved.is_file() and path_saved.read_bytes()==content,(case,"archive missing")
    assert source_rs["entry_count"]==len(entries_rs)
    return rust
csv_data="word,translation,definition,aliases,collins,frq,source_lang\nPCB trace,走线,printed circuit board line,Wire|Route,1,3,en\nresistor,电阻,Resistance,,,0,en\ncoil,电感,Inductor,,,0,en\n".encode()
check_case("csv","dict.csv",csv_data,"csv",source_lang="en",max_entries=2)
check_case("auto","dict_auto.csv",csv_data,"auto",source_lang="en")
check_case("tsv","dict.tsv","trace\t走线\nresistor\t电阻\n".encode(),"tsv",source_lang="en")
check_case("jsonl","dict.jsonl",(
    '{"word":"trace","lang_code":"en","translations":[{"lang_code":"zh","word":"走线"}],"senses":[{"glosses":["wiring"]}]}\n'
    '{"word":"resistor","lang_code":"en","translations":[{"lang_code":"zh","word":"电阻"}]}\n').encode(),"jsonl",source_lang="en")
check_case("cedict","dict.cedict","電路 电路 [dian4 lu4] /circuit/electricity/\n".encode(),"cc-cedict",source_lang="zh",target_lang="en")
check_case("tmx","dict.tmx",(
    '<tmx><body><tu><tuv xml:lang="en"><seg>trace</seg></tuv>'
    '<tuv xml:lang="zh"><seg>走线</seg></tuv></tu></body></tmx>').encode(),"tmx",source_lang="en")
check_case("tbx","dict.tbx",(
    '<tbx><conceptEntry><langSet xml:lang="en"><term>trace</term></langSet>'
    '<langSet xml:lang="zh"><term>走线</term></langSet></conceptEntry></tbx>').encode(),"tbx",source_lang="en")
check_case("preset","dict_ecdict.csv",csv_data,"ecdict",source_lang="",preset=True)
# A real replacement removes stale rows, and an upsert keeps stable dictionary IDs.
initial="term,translation\nA,甲\nB,乙\n".encode()
next_bytes="term,translation\nB,新的乙\nC,丙\n".encode()
def mutate(mode,data):
    result=call(data,"replace.csv",{"slug":"rs-gloss-replace","name":"Replace","format_name":"csv",
        "import_mode":mode,"source_lang":"en","target_lang":"zh","max_entries":"0"})
    assert result[0]==200,result
    assert result[1]["max_entries"]==250000,result
    return result[1]
mutate("upsert",initial)
with engine.connect() as con:
    before=dict(con.execute(text("SELECT normalized_term,id FROM translation_dictionary_entries WHERE source_id=(SELECT id FROM translation_dictionary_sources WHERE slug='rs-gloss-replace')")).all())
mutate("upsert",next_bytes)
with engine.connect() as con:
    after=dict(con.execute(text("SELECT normalized_term,id FROM translation_dictionary_entries WHERE source_id=(SELECT id FROM translation_dictionary_sources WHERE slug='rs-gloss-replace')")).all())
assert set(after)=={"a","b","c"} and after["b"]==before["b"],(before,after)
mutate("replace",next_bytes)
with engine.connect() as con:
    final=dict(con.execute(text("SELECT normalized_term,id FROM translation_dictionary_entries WHERE source_id=(SELECT id FROM translation_dictionary_sources WHERE slug='rs-gloss-replace')")).all())
assert set(final)=={"b","c"} and final["b"]!=after["b"]
for fields in ({"format_name":"unknown"},{"import_mode":"drop"},{"dictionary_preset":"fake"}):
    assert call(initial,"terms.csv",fields)[0]==400,fields
print("PASS DIFF: Python/Rust CSV/TSV/JSONL/CEDICT/TMX/TBX presets, SHA-256 archives, source/batch/entry writes, max_entries and upsert/replace")

# Compare lookup's actual edge-value behavior with the original implementation.
for q in (
    {"term":"  PCB___Trace  ","target_lang":"zh","source_lang":"en","limit":0,"min_quality":-1.0},
    {"term":"trace","target_lang":"zh","source_lang":"en","exact":False,"limit":500,"min_quality":-0.5},
    {"term":"resistor","target_lang":"zh","source_lang":"en","limit":500,"min_quality":2.0},
):
    with Session(engine) as db:
        expected=lookup_dictionary_entries(db,**q)
        db.rollback()
    request=Request(base+"/subtitle/dictionaries/lookup",method="POST",
        data=json.dumps(q).encode(),headers={"x-videoroll-v2-internal-token":token,"content-type":"application/json"})
    with urlopen(request,timeout=15) as result:
        actual=json.loads(result.read())
    assert actual["count"]==len(expected),(q,actual,expected)
    assert [str(r["id"]) for r in actual["results"]]==[str(r["id"]) for r in expected],q
print("PASS DIFF: original dictionary normalized lookup, bounded quality/limit and result ordering")
