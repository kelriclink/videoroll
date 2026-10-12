from pathlib import Path
import importlib.util
import sqlite3

SRC=Path(__file__).parents[1]/"scripts/native/migrate_ffplayout_paths.py"
spec=importlib.util.spec_from_file_location("native_ffplayout_paths",SRC)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def test_legacy_paths_are_mapped_without_modifying_auth(tmp_path):
    data=tmp_path/"data"
    folder=data/"ffplayout/db"
    folder.mkdir(parents=True)
    path=folder/"ffplayout.db"
    c=sqlite3.connect(path)
    c.execute("CREATE TABLE config_global (logs TEXT, playlists TEXT, public TEXT, storage TEXT)")
    c.execute("INSERT INTO config_global VALUES (?,?,?,?)",tuple("/var/lib/ffplayout/"+x for x in ("logs","playlists","public","media")))
    c.execute("CREATE TABLE channels (public TEXT,playlists TEXT,storage TEXT)")
    c.execute("INSERT INTO channels VALUES (?,?,?)",tuple("/var/lib/ffplayout/"+x for x in ("public","playlists","media")))
    c.execute("CREATE TABLE config_recording (path TEXT)")
    c.execute("INSERT INTO config_recording VALUES ('/var/lib/ffplayout/recordings/1')")
    c.execute("CREATE TABLE auth_user (password_hash TEXT)")
    c.execute("INSERT INTO auth_user VALUES ('untouched')")
    c.commit();c.close()
    plan=mod.migrate(path,data)
    assert sum(plan.values())==8
    c=sqlite3.connect(path)
    assert c.execute("SELECT storage FROM config_global").fetchone()[0]=="/var/lib/ffplayout/media"
    c.close()
    applied=mod.migrate(path,data,execute=True)
    assert sum(applied.values())==8
    c=sqlite3.connect(path)
    assert c.execute("SELECT storage FROM config_global").fetchone()[0]==str(data.resolve()/"ffplayout/media")
    assert c.execute("SELECT path FROM config_recording").fetchone()[0]==str(data.resolve()/"ffplayout/recordings/1")
    assert c.execute("SELECT password_hash FROM auth_user").fetchone()[0]=="untouched"
    c.close()
    assert sum(mod.migrate(path,data,execute=True).values())==0
