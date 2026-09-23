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

