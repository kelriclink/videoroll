"""Flatten legacy ffplayout VideoRoll/task-id/video files without breaking playlists.

Dry-run by default. The original file is retained, so historical playlists
continue resolving their former absolute media paths. New files are hardlinks
where supported; no re-encoding or bulk video copying is needed.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from videoroll.apps.orchestrator_api.services import playout_service
from videoroll.apps.subtitle_service.task_title_store import get_task_display_title
from videoroll.config import get_orchestrator_settings
from videoroll.db.models import PlayoutAssetLink
from videoroll.db.session import get_sessionmaker


def flatten(*, execute: bool = False) -> dict[str, int]:
    settings = get_orchestrator_settings()
    root = Path(settings.playout_media_root)
    counts = {"reviewed": 0, "flattened": 0, "already_flat": 0, "missing": 0, "errors": 0}
    factory = get_sessionmaker(settings.database_url)
    with factory() as db:
        identifiers = [row.id for row in db.query(PlayoutAssetLink.id).all()]
    for item_id in identifiers:
        with factory() as db:
            link = db.get(PlayoutAssetLink, item_id)
            if link is None:
                continue
            counts["reviewed"] += 1
            if len(Path(link.relative_media_path).parts) == 2:
                counts["already_flat"] += 1
                continue
            old = playout_service._resolve_media_path(root, link.relative_media_path)
            if not old.is_file():
                counts["missing"] += 1
                continue
            title = get_task_display_title(db, str(link.task_id))
            filename = playout_service._safe_media_filename(title, old.name, link.asset_id)
            target = playout_service._choose_media_path(db, root, filename, link.asset_id, link.source_checksum, old)
            if not execute:
                print(f"DRY-RUN {link.asset_id}: {link.relative_media_path} -> {target}")
                counts["flattened"] += 1
                continue
            try:
                destination = playout_service._resolve_media_path(root, target)
                playout_service._transfer(old, destination, link.source_checksum)
                link.relative_media_path = target
                db.add(link)
                db.commit()
                # Old file is deliberately not deleted. Live playlists may still
                # refer to the exact old path; hardlinks preserve both safely.
                print(f"MIGRATED {link.asset_id}: {target}")
                counts["flattened"] += 1
            except Exception as exc:
                db.rollback()
                counts["errors"] += 1
                print(f"ERROR {link.asset_id}: {type(exc).__name__}: {exc}")
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Apply verified hardlinks and update link paths")
    args = parser.parse_args()
    summary = flatten(execute=args.execute)
    print(summary)
    if summary["errors"]:
        raise SystemExit(1)
