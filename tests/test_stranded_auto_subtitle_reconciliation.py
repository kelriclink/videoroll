"""Regression tests for failed automatic pipeline subtitle-job reconciliation."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from videoroll.db.models import PipelineRun, SubtitleJob, SubtitleJobStatus
from videoroll.workflows import pipeline_status_sync as sync


class FakeQuery:
    def __init__(self, db, model):
        self.db, self.model = db, model

    def join(self, *args, **kwargs): return self
    def filter(self, *args, **kwargs): return self
    def order_by(self, *args, **kwargs): return self
    def limit(self, *args, **kwargs): return self
    def with_for_update(self, *args, **kwargs): return self

    def all(self):
        return self.db.jobs if self.model is SubtitleJob else []

    def first(self):
        return self.db.parent if self.model is PipelineRun else self.db.active_run


class FakeSession:
    def __init__(self, jobs, parent, active_run=None):
        self.jobs, self.parent, self.active_run = jobs, parent, active_run
        self.commits, self.additions = 0, []

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def query(self, model): return FakeQuery(self, model)
    def add(self, obj): self.additions.append(obj)
    def commit(self): self.commits += 1


class StrandedJobReconcileTests(unittest.TestCase):
    def run_case(self, *, state="failed", latest_is_newer=False,
                 finished_before_job=False, active=False, jobs=True):
        now = datetime.now(timezone.utc)
        task_id = uuid.uuid4()
        job = SimpleNamespace(
            task_id=task_id, status=SubtitleJobStatus.queued,
            created_at=now, progress=0, lease_owner=None,
            heartbeat_at=None, updated_at=now, error_message=None,
        )
        parent = SimpleNamespace(
            state=state,
            created_at=now + timedelta(seconds=1) if latest_is_newer
            else now - timedelta(seconds=30),
            finished_at=now - timedelta(seconds=1) if finished_before_job
            else now + timedelta(minutes=30),
        )
        db = FakeSession([job] if jobs else [], parent,
                         object() if active else None)
        with patch.object(sync, "get_sessionmaker", return_value=lambda: db):
            count = sync.reconcile_stranded_auto_subtitle_jobs(
                SimpleNamespace(database_url="unused")
            )
        return count, job, db

    def test_fail_only_untouched_job_on_failed_parent(self):
        count, job, db = self.run_case()
        self.assertEqual(count, 1)
        self.assertEqual(job.status, SubtitleJobStatus.failed)
        self.assertIn("saved source media", job.error_message)
        self.assertEqual(db.commits, 1)

    def test_live_parent_not_reconciled(self):
        count, job, db = self.run_case(state="running")
        self.assertEqual((count, job.status, db.commits), (0, SubtitleJobStatus.queued, 0))

    def test_newer_parent_not_reconciled(self):
        self.assertEqual(self.run_case(latest_is_newer=True)[0], 0)

    def test_failed_parent_before_job_creation_not_reconciled(self):
        self.assertEqual(self.run_case(finished_before_job=True)[0], 0)

    def test_active_other_run_not_reconciled(self):
        self.assertEqual(self.run_case(active=True)[0], 0)

    def test_no_candidates_is_noop(self):
        self.assertEqual(self.run_case(jobs=False)[0], 0)


if __name__ == "__main__":
    unittest.main()
