"""Read-only integrity and explicitly quiesced legacy partial recovery on isolated fixtures."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4
import psycopg

uri=os.environ["DATABASE_URL"]
assert urlsplit(uri).hostname=="127.0.0.1" and urlsplit(uri).path=="/videoroll_test"
exe=Path("rust/target/debug/videoroll-v2").resolve()

def run(root,*args,ok=True):
    result=subprocess.run([str(exe),*args],env={**os.environ,"VIDEOROLL_V2_STORAGE_ROOT":str(root)},
                          capture_output=True,text=True,timeout=30)
    if not ok:
        assert result.returncode!=0,result.stdout
        return
    assert result.returncode==0,(result.stdout,result.stderr)
    return json.loads(result.stdout)

with tempfile.TemporaryDirectory(prefix="vr-integrity-",dir=Path("rust/target").resolve()) as temp:
    parent=Path(temp); root=parent/"objects"; root.mkdir()
    partial=parent/".partial"; partial.mkdir()
    folder=root/"fixture"; folder.mkdir()
    def old(name):
        p=partial/name;p.write_bytes(b"stale")
        os.utime(p,(1,1));return p
    stale=old(".video.mp4.abcd1234.partial")
    locked=old(".locked.abc_1234.partial")
    hard=old(".hard.abcdef12.partial")
    (parent/"hard-copy").hardlink_to(hard)
    recent=partial/".recent.abcdef12.partial";recent.write_bytes(b"fresh")
    unrelated=old("video.mp4.part")
    odd=old(".video.mp4.12345.partial")
    (partial/".link.abcdef12.partial").symlink_to(stale)
    nested=partial/"nested";nested.mkdir();(nested/".video.abcdefgh.partial").write_bytes(b"nested")
    with locked.open("rb") as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before={p.name for p in partial.iterdir()}
        result=run(root,"legacy-partials","--limit","100")
        states={r["name"]:r["status"] for r in result["entries"]}
        assert states[stale.name]=="candidate" and states[locked.name]=="locked",states
        for p in (hard,recent,partial/".link.abcdef12.partial"):
            assert states[p.name]=="protected",states
        assert states[unrelated.name]==states[odd.name]=="not_legacy_upload"
        assert {p.name for p in partial.iterdir()}==before
        run(root,"legacy-partials","--delete",stale.name,ok=False)
        run(root,"legacy-partials","--delete","../"+stale.name,"--quiesced","--exclusive-root",ok=False)
        assert stale.exists()
        deleted=run(root,"legacy-partials","--delete",stale.name,"--delete",locked.name,
                    "--delete",hard.name,"--delete",recent.name,"--quiesced","--exclusive-root")
        assert {r["name"]:r["status"] for r in deleted["entries"]}=={stale.name:"removed",locked.name:"locked",hard.name:"protected",recent.name:"protected"}
        assert not stale.exists() and locked.exists() and hard.exists() and recent.exists() and unrelated.exists()
    partial.rename(parent/"saved-partials")
    partial.symlink_to(parent/"saved-partials",target_is_directory=True)
    run(root,"legacy-partials",ok=False)
    partial.unlink()

    good=b"good"; sha=hashlib.sha256(good).hexdigest()
    for n,content in (("good",good),("wrong",b"oops"),("size",b"x"),("conflict",good),("nohash",good),("subtitle",good),("orphan",b"unknown"),("pending",b"keep"),("active.part",b"checkpoint")):
        (folder/n).write_bytes(content)
    (folder/"link").symlink_to(parent/"hard-copy")
    os.mkfifo(folder/"fifo")
    (root/"unsafe").symlink_to(folder,target_is_directory=True)
    t=uuid4()
    with psycopg.connect(uri) as c:
        c.execute("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES(%s,'local','own','rendered','rust')",(t,))
        for n,checksum,size in (("good",sha,4),("wrong",sha,4),("size",sha,4),("missing",sha,4),("conflict",sha,4),("nohash",None,4),("link",sha,5),("fifo",None,0),("deleted",sha,4)):
            c.execute("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,retain_until,state) VALUES(%s,%s,%s,now(),%s)",("fixture/"+n,checksum,size,"deleted" if n=="deleted" else "live"))
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_raw','fixture/conflict',%s,4)",(uuid4(),t,"0"*64))
        c.execute("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES(%s,%s,'video_final','unsafe/good',%s,4)",(uuid4(),t,sha))
        c.execute("INSERT INTO subtitles(id,task_id,format,language,storage_key) VALUES(%s,%s,'srt','zh','fixture/subtitle')",(uuid4(),t))
        for bad in ("../escape",""):
            c.execute("INSERT INTO storage_objects(storage_key,size_bytes,retain_until) VALUES(%s,4,now())",(bad,))
        c.execute("INSERT INTO app_settings(key,value_json) VALUES('storage.pending_delete.fixture',%s::jsonb)",(json.dumps({"storage_key":"fixture/pending"}),))
    def snapshot():
        with psycopg.connect(uri) as c:
            return [c.execute("SELECT to_jsonb(x) FROM "+table+" x ORDER BY 1").fetchall() for table in ("tasks","assets","subtitles","storage_objects","app_settings")]
    original=snapshot()
    result=run(root,"storage-integrity-audit","--limit","100","--directory","fixture")
    expected={"good":"verified","wrong":"checksum_mismatch","size":"size_mismatch","missing":"missing","conflict":"metadata_conflict","nohash":"present_no_checksum","link":"not_regular_file","fifo":"not_regular_file","deleted":"registry_not_live","subtitle":"present_no_checksum"}
    states={r["key"]:r["status"] for r in result["entries"]}
    assert {k.removeprefix("fixture/"):v for k,v in states.items() if k.startswith("fixture/")}==expected,states
    assert states["unsafe/good"]==states["../escape"]==states[""]=="unsafe_path",states
    first=run(root,"storage-integrity-audit","--limit","1","--max-hash-mib","0")
    assert first["next_after"]=="" and first["entries"][0]["key"]==""
    second=run(root,"storage-integrity-audit","--limit","1","--max-hash-mib","0","--after","")
    assert second["next_after"]=="../escape"
    third=run(root,"storage-integrity-audit","--limit","1","--max-hash-mib","0","--after","../escape")
    assert third["entries"][0]["key"]=="fixture/conflict"
    assert run(root,"storage-integrity-audit","--limit","1","--directory","fixture")["directory_truncated"] is True
    assert result["read_only"] and result["unregistered_candidates"]==["fixture/orphan"],result
    assert result["hash_bytes_budget_used"]==8,result
    assert run(root,"storage-integrity-audit","--max-hash-mib","0")["counts"]["hash_deferred"]==2
    seen=[];after=None
    while True:
        args=["storage-integrity-audit","--limit","3","--max-hash-mib","0"]
        if after is not None:args+=["--after",after]
        page=run(root,*args);seen.extend(e["key"] for e in page["entries"])
        after=page["next_after"]
        if after is None:break
    assert seen==sorted(states),seen
    assert snapshot()==original
    assert (folder/"orphan").read_bytes()==b"unknown" and (folder/"wrong").read_bytes()==b"oops"
    print("PASS: original partial audit/offline deletion, lock/age/link/path safeguards; registered missing/checksum/size/conflict audit, legacy asset/subtitle coverage, shallow orphan candidates, byte budget, keyset pagination, no database or media mutation")
