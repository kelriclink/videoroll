"""Control-plane tests for VideoRoll-managed native YouDub engine."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videoroll.apps.dubbing import youdub_control as control
from videoroll.db.models import Base, DubbingJob, Task, SubtitleJob, TaskStatus, SourceType, SourceLicense, SubtitleJobStatus


def _database():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_native_control_is_off_by_default_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(control, "model_installed", lambda: True)
    factory = _database()
    with factory() as db:
        assert control.status(db)["enabled"] is False
        enabled = control.configure(db, enabled=True)
        assert enabled["accepting_jobs"] is True
        assert enabled["active_jobs"] == 0
        assert enabled["gpu_loaded"] is False  # no resident model process
        assert control.is_enabled() is True
        assert control.configure(db, enabled=False)["enabled"] is False
        assert control.is_enabled() is False


def test_native_control_rejects_missing_model(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(control, "model_installed", lambda: False)
    factory = _database()
    with factory() as db:
        with pytest.raises(ValueError, match="not ready"):
            control.configure(db, enabled=True)
        assert not control.status(db)["accepting_jobs"]


def test_native_control_cannot_strand_active_or_queued_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(control, "model_installed", lambda: True)
    factory = _database()
    task_id, subtitle_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.youtube,
                    source_license=SourceLicense.own, status=TaskStatus.rendered))
        db.add(SubtitleJob(id=subtitle_id, task_id=task_id,
                           status=SubtitleJobStatus.succeeded, request_json={}))
        db.add(DubbingJob(
            id=job_id, task_id=task_id, subtitle_job_id=subtitle_id,
            status="queued", progress=0, request_json={
                "model_path": control.MODEL_ID,
            }, operation_key="unit-native", output_json={},
        ))
        db.commit()
        control.configure(db, enabled=True)
        assert control.status(db)["active_jobs"] == 1
        with pytest.raises(RuntimeError, match="queued/running"):
            control.configure(db, enabled=False)
        assert control.is_enabled()
        job = db.get(DubbingJob, job_id)
        job.status = "succeeded"
        db.commit()
        assert control.configure(db, enabled=False)["active_jobs"] == 0


def test_old_engine_jobs_do_not_block_native_control(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(control, "model_installed", lambda: True)
    factory = _database()
    task_id, subtitle_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.youtube,
                    source_license=SourceLicense.own, status=TaskStatus.rendered))
        db.add(SubtitleJob(id=subtitle_id, task_id=task_id,
                           status=SubtitleJobStatus.succeeded, request_json={}))
        db.add(DubbingJob(id=job_id, task_id=task_id, subtitle_job_id=subtitle_id,
                          status="queued", progress=0, request_json={
                              "model_path": "cosyvoice3:base",
                          }, operation_key="unit-legacy", output_json={}))
        db.commit()
        control.configure(db, enabled=True)
        assert control.configure(db, enabled=False)["active_jobs"] == 0
