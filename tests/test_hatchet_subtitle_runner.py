from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from videoroll.apps.subtitle_service import worker
from videoroll.db.base import Base
from videoroll.db.models import SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


class _FakeStore:
    def __init__(self, _settings: object) -> None:
        pass

    def ensure_ready(self) -> None:
        pass


class _NoopHeartbeat:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def test_hatchet_runner_retries_instead_of_accepting_live_foreign_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id,
            request_json={"task_id": str(task.id)},
            status=SubtitleJobStatus.running,
            lease_owner="old-hatchet-worker",
            lease_until=datetime.now(timezone.utc) + timedelta(seconds=45),
            progress=25,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(worker, "_db", factory)
    monkeypatch.setattr(worker, "FileStore", _FakeStore)

    with pytest.raises(worker.SubtitleJobLeaseBusy, match="still leased"):
        worker.run_subtitle_job(
            str(job_id),
            retry_attempt=1,
            raise_on_error=True,
        )

    with factory() as db:
        persisted = db.get(SubtitleJob, job_id)
        assert persisted is not None
        assert persisted.status == SubtitleJobStatus.running
        assert persisted.lease_owner == "old-hatchet-worker"
    engine.dispose()


def test_hatchet_runner_persists_terminal_failure_with_fresh_session(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id,
            request_json={"task_id": str(task.id)},
            status=SubtitleJobStatus.queued,
        )
        db.add(job)
        db.commit()
        job_id = job.id
        task_id = task.id

    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(worker, "_db", factory)
    monkeypatch.setattr(worker, "FileStore", _FakeStore)
    monkeypatch.setattr(worker, "JobLeaseHeartbeat", _NoopHeartbeat)
    monkeypatch.setattr(worker, "release_job_lease", lambda *_args, **_kwargs: True)

    with pytest.raises(worker.SubtitleJobExecutionFailed, match="missing input.key"):
        worker.run_subtitle_job(str(job_id), raise_on_error=True)

    with factory() as db:
        persisted_job = db.get(SubtitleJob, job_id)
        persisted_task = db.get(Task, task_id)
        assert persisted_job is not None
        assert persisted_job.status == SubtitleJobStatus.failed
        assert persisted_job.error_message == "missing input.key"
        assert persisted_task is not None
        assert persisted_task.status.value == "FAILED"
        assert persisted_task.error_code == "SUBTITLE_FAILED"
        assert persisted_task.error_message == "missing input.key"

    engine.dispose()

