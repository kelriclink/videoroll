from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from videoroll.utils.workdir_maintenance import (
    WorkdirJobState,
    cleanup_reclaimable_dirs,
    scan_workdir,
)


class WorkdirMaintenanceTests(unittest.TestCase):
    def test_scan_marks_only_inactive_or_missing_dirs_reclaimable(self) -> None:
        now = datetime(2100, 1, 1, 0, 0, tzinfo=timezone.utc)
        subtitle_running = uuid.uuid4()
        subtitle_failed = uuid.uuid4()
        render_done = uuid.uuid4()
        youtube_active = uuid.uuid4()
        youtube_idle = uuid.uuid4()
        missing_task = uuid.uuid4()
        task_a = uuid.uuid4()
        task_b = uuid.uuid4()
        task_c = uuid.uuid4()

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            self._write_bytes(work_dir / "subtitle" / str(subtitle_running) / "input.mp4", 8)
            self._write_bytes(work_dir / "subtitle" / str(subtitle_failed) / "audio.wav", 4)
            self._write_bytes(work_dir / "render" / str(render_done) / "video.mp4", 16)
            self._write_bytes(work_dir / "youtube" / str(youtube_active) / "cookie.txt", 2)
            self._write_bytes(work_dir / "youtube" / str(youtube_idle) / "meta.json", 3)
            self._write_bytes(work_dir / "youtube" / str(missing_task) / "meta.json", 5)

            result = scan_workdir(
                work_dir,
                subtitle_jobs={
                    subtitle_running: WorkdirJobState(task_id=task_a, status="running"),
                    subtitle_failed: WorkdirJobState(task_id=task_b, status="failed"),
                },
                render_jobs={
                    render_done: WorkdirJobState(task_id=task_c, status="succeeded"),
                },
                known_task_ids={youtube_active, youtube_idle},
                active_task_ids={youtube_active},
                now=now,
                recent_grace_seconds=0,
            )

        by_path = {item.rel_path: item for item in result.entries}

        self.assertEqual(result.scanned_dirs, 6)
        self.assertFalse(by_path[f"subtitle/{subtitle_running}"].reclaimable)
        self.assertEqual(by_path[f"subtitle/{subtitle_running}"].reason, "subtitle job active (running)")

        self.assertTrue(by_path[f"subtitle/{subtitle_failed}"].reclaimable)
        self.assertEqual(by_path[f"subtitle/{subtitle_failed}"].reason, "subtitle job failed")

        self.assertTrue(by_path[f"render/{render_done}"].reclaimable)
        self.assertEqual(by_path[f"render/{render_done}"].reason, "render job succeeded")

        self.assertFalse(by_path[f"youtube/{youtube_active}"].reclaimable)
        self.assertEqual(by_path[f"youtube/{youtube_active}"].reason, "task active")

        self.assertTrue(by_path[f"youtube/{youtube_idle}"].reclaimable)
        self.assertEqual(by_path[f"youtube/{youtube_idle}"].reason, "youtube temp directory idle")

        self.assertTrue(by_path[f"youtube/{missing_task}"].reclaimable)
        self.assertEqual(by_path[f"youtube/{missing_task}"].reason, "youtube task missing")

    def test_cleanup_removes_only_reclaimable_dirs(self) -> None:
        now = datetime(2100, 1, 1, 0, 0, tzinfo=timezone.utc)
        running_job = uuid.uuid4()
        failed_job = uuid.uuid4()
        task_a = uuid.uuid4()
        task_b = uuid.uuid4()

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            keep_dir = work_dir / "subtitle" / str(running_job)
            delete_dir = work_dir / "subtitle" / str(failed_job)
            self._write_bytes(keep_dir / "keep.bin", 7)
            self._write_bytes(delete_dir / "drop.bin", 11)

            scan = scan_workdir(
                work_dir,
                subtitle_jobs={
                    running_job: WorkdirJobState(task_id=task_a, status="running"),
                    failed_job: WorkdirJobState(task_id=task_b, status="failed"),
                },
                render_jobs={},
                known_task_ids=set(),
                active_task_ids=set(),
                now=now,
                recent_grace_seconds=0,
            )
            cleanup = cleanup_reclaimable_dirs(work_dir, scan.entries)

            self.assertEqual(cleanup.deleted_dirs, 1)
            self.assertEqual(cleanup.deleted_paths, [f"subtitle/{failed_job}"])
            self.assertTrue(keep_dir.exists())
            self.assertFalse(delete_dir.exists())
            self.assertEqual(cleanup.errors, [])

    @staticmethod
    def _write_bytes(path: Path, size: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


if __name__ == "__main__":
    unittest.main()
