from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videoroll.apps.orchestrator_api.routers.tasks import retry_dubbing_job
from videoroll.db.models import Base, DubbingJob, SourceLicense, SourceType, Task, TaskStatus


def _init():
    db = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(db)
    factory = sessionmaker(db, expire_on_commit=False)
    task_id, job_id = uuid.uuid4(), uuid.uuid4()
    with factory() as session:
        session.add(Task(id=task_id, source_type=SourceType.youtube,
                         source_license=SourceLicense.own,
                         status=TaskStatus.rendered))
        session.add(DubbingJob(id=job_id, task_id=task_id, status="failed",
                               request_json={}, output_json={}))
        session.commit()
    return factory, task_id, job_id


class DummyLauncher:
    runs = []

    def __init__(self, _settings):
        pass

    def launch_dubbing_job(self, job_id, *, dispatch_key=None):
        self.runs.append((job_id, dispatch_key))


def test_failed_dubbing_job_can_relaunch_without_new_job_id():
    factory, task_id, job_id = _init()
    DummyLauncher.runs = []
    with patch("videoroll.workflows.launcher.PipelineLauncher", DummyLauncher), patch(
        "videoroll.apps.dubbing.worker_status.worker_status", return_value={"available": True, "state": "active"}
    ):
        with factory() as db:
            result = retry_dubbing_job(task_id, job_id, settings=SimpleNamespace(), db=db)
            assert result.status == "queued"
            assert result.job_id == job_id
            assert db.get(DubbingJob, job_id).status == "queued"
    assert len(DummyLauncher.runs) == 1
    assert str(job_id) in DummyLauncher.runs[0][1]


def test_live_lease_refuses_retry_but_expired_one_can_recover():
    factory, task_id, job_id = _init()
    DummyLauncher.runs = []
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        job.status = "synthesizing"
        job.lease_owner = "current-worker"
        job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        db.commit()
        with pytest.raises(HTTPException) as err:
            retry_dubbing_job(task_id, job_id, settings=SimpleNamespace(), db=db)
        assert err.value.status_code == 409
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        job.lease_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    with patch("videoroll.workflows.launcher.PipelineLauncher", DummyLauncher), patch(
        "videoroll.apps.dubbing.worker_status.worker_status", return_value={"available": True, "state": "active"}
    ):
        with factory() as db:
            result = retry_dubbing_job(task_id, job_id, settings=SimpleNamespace(), db=db)
            assert result.status == "queued"
            assert db.get(DubbingJob, job_id).lease_owner is None


def test_finished_dubbing_job_is_not_redispatched():
    factory, task_id, job_id = _init()
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        job.status = "succeeded"
        db.commit()
        with pytest.raises(HTTPException) as err:
            retry_dubbing_job(task_id, job_id, settings=SimpleNamespace(), db=db)
        assert err.value.status_code == 409
