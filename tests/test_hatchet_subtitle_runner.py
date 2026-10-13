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

def test_asr_cancellation_callback_does_not_reopen_primary_db_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import shutil
    from videoroll.db.models import Asset

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__, Asset.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(task_id=task.id, status=SubtitleJobStatus.queued, request_json={
            "input": {"key": "video.mp4"},
            "youtube_subtitle_mode": "off",
            "runtime_profile": False,
            "translate": {"enabled": False},
            "output": {"formats": ["srt"]},
        })
        db.add(job)
        db.commit()
        job_id = job.id

    (tmp_path / "video.mp4").write_bytes(b"video")

    class _LocalStore(_FakeStore):
        def path_for(self, key, **_kwargs):
            return tmp_path / key

        def download_file(self, *_args, **_kwargs):
            raise FileNotFoundError("not saved")

        def upload_file(self, *_args, **_kwargs):
            pass

        def delete_object(self, *_args, **_kwargs):
            pass

        def promote_file(self, source, key):
            target = self.path_for(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def extract_audio(_ffmpeg, _video, output, **_kwargs):
        output.write_bytes(b"audio")

    callback_invoked = []

    def run_asr_stage(*, db, cancel_check, **_kwargs):
        # Simulate the real ASR settings lookup followed by _end_read_transaction.
        db.rollback()
        assert not db.in_transaction()
        cancel_check()
        assert not db.in_transaction(), "cancel callback reopened an idle read transaction"
        callback_invoked.append(True)
        raise RuntimeError("Infer Request is busy")

    monkeypatch.setattr(worker.settings, "work_dir", str(tmp_path))
    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(worker, "_db", factory)
    monkeypatch.setattr(worker, "FileStore", _LocalStore)
    monkeypatch.setattr(worker, "JobLeaseHeartbeat", _NoopHeartbeat)
    monkeypatch.setattr(worker, "extract_audio", extract_audio)
    monkeypatch.setattr(worker, "_run_asr_stage", run_asr_stage)
    monkeypatch.setattr(worker, "publish_log_updated", lambda *_args, **_kwargs: None)

    with pytest.raises(worker.SubtitleJobTransientFailure, match="Infer Request is busy"):
        worker.run_subtitle_job(str(job_id), retry_attempt=0, raise_on_error=True)
    assert callback_invoked == [True]
    engine.dispose()


@pytest.mark.parametrize("retry_attempt,expected_status", [
    (0, SubtitleJobStatus.queued),
    (5, SubtitleJobStatus.failed),
])
def test_temporary_asr_failure_requeues_before_retry_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path, retry_attempt: int, expected_status: SubtitleJobStatus,
) -> None:
    from videoroll.db.models import Asset, TaskStatus

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, SubtitleJob.__table__, Asset.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id,
            request_json={
                "task_id": str(task.id),
                "input": {"key": "video.mp4"},
                "youtube_subtitle_mode": "off",
                "runtime_profile": False,
                "translate": {"enabled": False},
                "output": {"formats": ["srt"]},
            },
            status=SubtitleJobStatus.queued,
        )
        db.add(job)
        db.commit()
        job_id, task_id = job.id, task.id

    (tmp_path / "video.mp4").write_bytes(b"fake video")

    class _LocalStore(_FakeStore):
        def path_for(self, key, **_kwargs):
            return tmp_path / key

        def download_file(self, *_args, **_kwargs):
            raise FileNotFoundError("no saved log")

        def upload_file(self, *_args, **_kwargs):
            pass

        def delete_object(self, *_args, **_kwargs):
            pass

    def busy(*_args, **_kwargs):
        raise RuntimeError("Infer Request is busy")

    monkeypatch.setattr(worker.settings, "work_dir", str(tmp_path))
    monkeypatch.setattr(worker, "_ensure_db", lambda: None)
    monkeypatch.setattr(worker, "_db", factory)
    monkeypatch.setattr(worker, "FileStore", _LocalStore)
    monkeypatch.setattr(worker, "JobLeaseHeartbeat", _NoopHeartbeat)
    monkeypatch.setattr(worker, "extract_audio", busy)
    expected_exception = (
        worker.SubtitleJobTransientFailure if retry_attempt < 5 else worker.SubtitleJobExecutionFailed
    )

    with pytest.raises(expected_exception, match="Infer Request is busy"):
        worker.run_subtitle_job(str(job_id), retry_attempt=retry_attempt, raise_on_error=True)

    with factory() as db:
        persisted_job = db.get(SubtitleJob, job_id)
        persisted_task = db.get(Task, task_id)
        assert persisted_job is not None
        assert persisted_job.status == expected_status
        assert persisted_job.error_message == "Infer Request is busy"
        assert persisted_task is not None
        if retry_attempt < 5:
            assert persisted_task.status != TaskStatus.failed
            assert persisted_job.request_json["resume"] is True
        else:
            assert persisted_task.status == TaskStatus.failed
    engine.dispose()
