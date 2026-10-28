"""Regression tests for durable, throttled, API-independent YouTube downloads."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from videoroll.db.base import Base
from videoroll.db.models import AppSetting, OperationInbox, PipelineRun, Task, SourceType, SourceLicense, TaskStatus
from videoroll.apps.youtube_ingest.download_dispatch import (
    DownloadSupervisor, download_operation_status, enqueue_download,
    read_download_concurrency, set_download_concurrency,
)


@compiles(JSONB, 'sqlite')
def _jsonb_as_sqlite(_type, _compiler, **kw):
    return 'JSON'


@pytest.fixture()
def factory():
    engine = create_engine('sqlite:///:memory:')
    tables = [Task.__table__, AppSetting.__table__, OperationInbox.__table__, PipelineRun.__table__]
    Base.metadata.create_all(engine, tables=tables)
    maker = sessionmaker(engine, expire_on_commit=False)
    yield maker
    Base.metadata.drop_all(engine, tables=list(reversed(tables)))
    engine.dispose()


def make_task(db, status=TaskStatus.ingested, priority=0):
    task = Task(source_type=SourceType.youtube, source_license=SourceLicense.authorized,
                source_url='https://www.youtube.com/watch?v=9bZkp7q19f0', status=status,
                priority=priority)
    db.add(task)
    db.flush()
    return task


def test_default_persistent_download_limit_and_validation(factory):
    with factory() as db:
        assert read_download_concurrency(db) == 2
        assert set_download_concurrency(db, 4) == 4
        assert read_download_concurrency(db) == 4
        for value in (0, 17, -5):
            with pytest.raises(ValueError):
                set_download_concurrency(db, value)
    with factory() as db:
        assert read_download_concurrency(db) == 4


def test_idempotent_durable_enqueue_does_not_launch_in_http_process(factory):
    with factory() as db:
        task = make_task(db)
        key = 'youtube-download:' + str(task.id)
        assert enqueue_download(db, task.id) == 'queued'
        assert enqueue_download(db, task.id) == 'queued'
        assert db.query(OperationInbox).filter_by(operation_key=key).count() == 1
        op = db.query(OperationInbox).filter_by(operation_key=key).one()
        assert op.status == 'pending'
        assert op.lease_owner is None


def test_expired_lease_is_queued_and_live_lease_is_active(factory):
    with factory() as db:
        task = make_task(db)
        enqueue_download(db, task.id)
        op = db.query(OperationInbox).filter_by(operation_key='youtube-download:' + str(task.id)).one()
        op.status = 'processing'
        op.lease_owner = 'dead-api-pid'
        op.lease_until = datetime.now(timezone.utc)-timedelta(hours=6)
        db.commit()
        assert download_operation_status(db, task.id)[0] == 'queued'
        op.lease_until = datetime.now(timezone.utc)+timedelta(minutes=5)
        db.commit()
        assert download_operation_status(db, task.id)[0] == 'processing'


def test_supervisor_recovery_filters_terminal_and_respects_priority(factory, monkeypatch):
    from videoroll.apps.youtube_ingest import download_dispatch as dispatch
    monkeypatch.setattr(dispatch, 'get_orchestrator_settings', lambda: SimpleNamespace(database_url='fake'))
    monkeypatch.setattr(dispatch, 'get_sessionmaker', lambda _: factory)
    supervisor = DownloadSupervisor()
    try:
        with factory() as db:
            old = make_task(db, priority=0)
            urgent = make_task(db, priority=100)
            done = make_task(db, status=TaskStatus.published, priority=200)
            for task in (old, urgent, done):
                enqueue = OperationInbox(operation_key='youtube-download:' + str(task.id),status='pending',request_json={})
                db.add(enqueue)
                if task.status == TaskStatus.ingested:
                    db.add(PipelineRun(task_id=task.id, workflow_name="VideoPipelineV1", engine="hatchet",
                                       state="running", request_json={}))
            db.commit()
            ids = supervisor._candidates(db, limit=2)
            assert ids == [urgent.id, old.id]
            assert done.id not in ids
    finally:
        supervisor.stop()


def test_orphaned_auto_pipeline_retries_once_and_never_repeats(factory, monkeypatch):
    from videoroll.workflows import pipeline_status_sync as sync
    from videoroll.workflows import launcher as launcher_module
    now = datetime.now(timezone.utc)
    with factory() as db:
        task = make_task(db)
        task.created_by = 'auto_youtube;auto_publish=;run_id=test'
        db.flush()
        op = OperationInbox(operation_key='youtube-download:' + str(task.id),
                            status='processing',lease_owner='defunct-api',
                            lease_until=now-timedelta(hours=1),
                            request_json={'task_id':str(task.id)})
        db.add_all([op, PipelineRun(task_id=task.id,workflow_name='VideoPipelineV1',
                                   engine='hatchet',state='failed',request_json={})])
        db.commit()
        task_id=task.id
    calls=[]
    monkeypatch.setattr(sync, 'get_sessionmaker', lambda _url: factory)
    monkeypatch.setattr(launcher_module, 'PipelineLauncher',
                        lambda _settings:SimpleNamespace(launch_auto_youtube=lambda task_id:calls.append(task_id)))
    settings=SimpleNamespace(database_url='sqlite-test')
    assert sync.recover_orphaned_youtube_download_pipelines(settings,limit=3)=={'recovered':1,'errors':0}
    assert sync.recover_orphaned_youtube_download_pipelines(settings,limit=3)=={'recovered':0,'errors':0}
    assert calls==[task_id]
    with factory() as db:
        row=db.query(OperationInbox).one()
        assert row.request_json['auto_workflow_recoveries']==1


def test_queued_download_progress_contract_accepts_recovery_states():
    from videoroll.apps.orchestrator_api.schemas import YouTubeDownloadProgressRead
    task_id=uuid.uuid4()
    assert YouTubeDownloadProgressRead(task_id=task_id,status='queued').status=='queued'
    assert YouTubeDownloadProgressRead(task_id=task_id,status='storing').status=='storing'
