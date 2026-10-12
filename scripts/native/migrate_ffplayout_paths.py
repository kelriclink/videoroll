#!/usr/bin/env python3
"""Safely remap legacy ffplayout SQLite data paths to an explicit native data root.

Run only while ffplayout is stopped, after making a database backup. Idempotent.
This updates path columns, never auth tables, playlists, media, credentials or logs.
"""
from __future__ import annotations
import argparse
import sqlite3
from pathlib import Path

TABLE_COLUMNS = {
    "config_global": ("logs", "playlists", "public", "storage"),
    "config_recording": ("path",),
    "channels": ("public", "playlists", "storage"),
}
OLD_PREFIX = "/var/lib/ffplayout/"

def migrate(db_path: Path, data_root: Path, *, execute: bool = False) -> dict[str,int]:
    if not db_path.is_file():
        raise ValueError(f"Missing ffplayout SQLite database: {db_path}")
    target = data_root.resolve() / "ffplayout"
    if not target.is_dir():
        raise ValueError(f"Missing destination ffplayout data: {target}")
    connection=sqlite3.connect(str(db_path),timeout=15)
    counts={}
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table, columns in TABLE_COLUMNS.items():
            for column in columns:
                count=connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" LIKE ?',
                    (OLD_PREFIX+'%',)
                ).fetchone()[0]
                counts[f"{table}.{column}"]=count
                if count and execute:
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}"=replace("{column}",?,?) WHERE "{column}" LIKE ?',
                        (OLD_PREFIX,str(target)+'/',OLD_PREFIX+'%')
                    )
        if execute:
            connection.commit()
        else:
            connection.rollback()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return counts

def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root",required=True,type=Path)
    parser.add_argument("--db",type=Path)
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args()
    db=args.db or args.data_root / "ffplayout/db/ffplayout.db"
    results=migrate(db,args.data_root,execute=args.execute)
    print(("Updated" if args.execute else "Would update"),sum(results.values()),"stored legacy ffplayout paths",results)

if __name__=="__main__":main()
