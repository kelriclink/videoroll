from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from videoroll.db.base import Base
from videoroll.db.models import PipelineRun, SourceLicense, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.workflows import pipeline_status_sync as sync


@compiles(JSONB, 'sqlite')
def _sqlite_jsonb(_type, _compiler, **kwargs):
    return 'JSON'


def _factory():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Task.__table__, PipelineRun.__table__, SubtitleJob.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.authorized,
                    status=TaskStatus.published)
        db.add(task)
        db.flush()
        for idx, state in enumerate(('submitted', 'submitted', 'submitted', 'succeeded', 'submitted')):
            db.add(PipelineRun(
                task_id=task.id, engine='hatchet', workflow_name='VideoPipelineV1',
                state=state, external_run_id=f'external-{idx}',
                request_json={'dispatch_key': f'auto-youtube:attempt-{idx}'},
            ))
        db.commit()
        tid = task.id
    return engine, factory, tid


def _runs(factory):
    with factory() as db:
        return {x.external_run_id: (x.state, x.finished_at, x.error_message) for x in db.query(PipelineRun).all()}


def _settings():
    return SimpleNamespace(database_url='unit-test-db', workflow_service_url='http://workflow:8030',
                           internal_api_secret='unit-test-secret', development_mode=False)


def test_exact_attempt_callback_preserves_other_attempts_and_business_state(monkeypatch):
    engine, factory, task_id = _factory()
    monkeypatch.setattr(sync, 'get_sessionmaker', lambda url: factory)
    from videoroll import config
    monkeypatch.setattr(config, 'get_subtitle_settings', lambda: SimpleNamespace(database_url='unit-test-db'))
    assert sync.finish_auto_pipeline_audit(str(task_id), 'auto-youtube:attempt-0', succeeded=True)
    assert sync.finish_auto_pipeline_audit(str(task_id), 'auto-youtube:attempt-1', succeeded=False)
    assert not sync.finish_auto_pipeline_audit(str(task_id), 'auto-youtube:attempt-0', succeeded=False)
    assert not sync.finish_auto_pipeline_audit(str(task_id), 'auto-youtube:unknown', succeeded=True)
    rows = _runs(factory)
    assert rows['external-0'][0] == 'succeeded'
    assert rows['external-0'][1] is not None
    assert rows['external-1'][0] == 'failed'
    assert 'Hatchet' in rows['external-1'][2]
    assert rows['external-2'][0] == 'submitted'
    with factory() as db:
        assert db.get(Task, task_id).status == TaskStatus.published
    engine.dispose()


def test_reconcile_authoritative_success_failure_cancel_and_running(monkeypatch):
    engine, factory, task_id = _factory()
    monkeypatch.setattr(sync, 'get_sessionmaker', lambda url: factory)
    statuses = {
        'external-0': 'COMPLETED', 'external-1': 'FAILED',
        'external-2': 'CANCELLED', 'external-4': 'RUNNING',
    }
    seen = []

    def handler(request):
        external = request.url.path.split('/')[-2]
        seen.append(external)
        return httpx.Response(200, json={'status': statuses[external]})

    real_client = httpx.Client
    monkeypatch.setattr(sync.httpx, 'Client', lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), timeout=kwargs.get('timeout', 8)))
    result = sync.reconcile_auto_pipeline_runs(_settings())
    assert result == {'checked': 4, 'updated': 4, 'errors': 0, 'stranded_jobs_failed': 0}
    assert 'external-3' not in seen
    rows = _runs(factory)
    assert [rows[f'external-{i}'][0] for i in range(5)] == [
        'succeeded', 'failed', 'canceled', 'succeeded', 'running']
    assert all(rows[f'external-{i}'][1] is not None for i in range(3))
    assert rows['external-4'][1] is None
    result = sync.reconcile_auto_pipeline_runs(_settings())
    assert result['updated'] == 0
    with factory() as db:
        assert db.get(Task, task_id).status == TaskStatus.published
    engine.dispose()


def test_reconcile_completed_subtitle_child_workflow(monkeypatch):
    engine, factory, task_id = _factory()
    with factory() as db:
        db.add(PipelineRun(
            task_id=task_id, engine="hatchet", workflow_name="SubtitleJobV1",
            state="submitted", external_run_id="subtitle-external",
            request_json={"dispatch_key": "subtitle-test"},
        ))
        db.commit()
    monkeypatch.setattr(sync, 'get_sessionmaker', lambda url: factory)
    real_client = httpx.Client
    monkeypatch.setattr(sync.httpx, 'Client', lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json={'status': 'COMPLETED' if 'subtitle-external' in str(req.url) else 'RUNNING'})), timeout=8))
    result = sync.reconcile_auto_pipeline_runs(_settings())
    assert result['checked'] == 5 and result['updated'] >= 1
    assert _runs(factory)['subtitle-external'][0] == 'succeeded'
    engine.dispose()


def test_reconcile_failed_http_never_marks_a_completed_business_task(monkeypatch):
    engine, factory, _ = _factory()
    monkeypatch.setattr(sync, 'get_sessionmaker', lambda url: factory)
    real_client = httpx.Client
    monkeypatch.setattr(sync.httpx, 'Client', lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), timeout=8))
    result = sync.reconcile_auto_pipeline_runs(_settings())
    assert result['errors'] == 4 and result['updated'] == 0
    assert _runs(factory)['external-0'][0] == 'submitted'
    engine.dispose()

def test_failed_auto_launch_recovery_reuses_durable_attempt(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from videoroll.workflows import launcher as launch_module

    engine, factory, _ = _factory()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    with factory() as db:
        auto = Task(source_type=SourceType.youtube, source_license=SourceLicense.authorized,
                    status=TaskStatus.ingested,
                    created_by="auto_youtube;auto_publish=;run_id=test")
        manual = Task(source_type=SourceType.youtube, source_license=SourceLicense.authorized,
                      status=TaskStatus.ingested, created_by="manual")
        db.add_all([auto, manual])
        db.flush()
        for task in (auto, manual):
            db.add(PipelineRun(task_id=task.id, engine="hatchet",
                               workflow_name="VideoPipelineV1", state="launch_failed",
                               created_at=old, updated_at=old,
                               request_json={"dispatch_key": f"auto-youtube:{task.id}"}))
        db.commit()
        auto_id, manual_id = auto.id, manual.id

    seen = []
    monkeypatch.setattr(sync, "get_sessionmaker", lambda url: factory)
    monkeypatch.setattr(launch_module, "PipelineLauncher",
                        lambda settings: SimpleNamespace(launch_auto_youtube=lambda task_id: seen.append(task_id)))
    result = sync.recover_failed_auto_launches(_settings())
    assert result == {"checked": 1, "launched": 1, "errors": 0}
    assert seen == [auto_id]
    assert manual_id not in seen
    with factory() as db:
        original = db.query(PipelineRun).filter(PipelineRun.task_id == auto_id).one()
        assert original.request_json["dispatch_key"] == f"auto-youtube:{auto_id}"
        assert db.query(Task).filter(Task.id == auto_id).count() == 1
    engine.dispose()


def test_failed_child_launch_recovery_reuses_original_dispatch_key(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from videoroll.workflows import launcher as launch_module

    engine, factory, _ = _factory()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    with factory() as db:
        task = Task(
            source_type=SourceType.youtube,
            source_license=SourceLicense.authorized,
            status=TaskStatus.ingested,
        )
        db.add(task)
        db.flush()
        job = SubtitleJob(
            task_id=task.id,
            status=SubtitleJobStatus.queued,
            request_json={},
            progress=0,
        )
        db.add(job)
        db.flush()
        db.add(
            PipelineRun(
                task_id=task.id,
                engine="hatchet",
                workflow_name="SubtitleJobV1",
                state="launch_failed",
                created_at=old,
                updated_at=old,
                request_json={
                    "job_id": str(job.id),
                    "dispatch_key": "subtitle-stable-key",
                },
            )
        )
        db.commit()
        job_id = job.id

    seen = []
    monkeypatch.setattr(sync, "get_sessionmaker", lambda _url: factory)
    monkeypatch.setattr(
        launch_module,
        "PipelineLauncher",
        lambda _settings: SimpleNamespace(
            launch_subtitle_job=lambda current, dispatch_key=None: seen.append(
                (current, dispatch_key)
            ),
            launch_dubbing_job=lambda *_args, **_kwargs: None,
        ),
    )

    result = sync.recover_failed_child_launches(_settings())

    assert result == {"checked": 1, "launched": 1, "errors": 0}
    assert seen == [(job_id, "subtitle-stable-key")]
    engine.dispose()


def test_failed_hatchet_child_reconciles_its_orphaned_queued_job(monkeypatch):
    from videoroll.db.models import SubtitleJobStatus

    engine, factory, _ = _factory()
    with factory() as db:
        task = Task(source_type=SourceType.youtube, source_license=SourceLicense.authorized,
                    status=TaskStatus.failed)
        db.add(task)
        db.flush()
        failed_job = SubtitleJob(task_id=task.id, status=SubtitleJobStatus.queued,
                                 request_json={"runtime_profile": True}, progress=0)
        db.add(failed_job)
        db.flush()
        job_id = failed_job.id
        db.add(PipelineRun(task_id=task.id, engine="hatchet", workflow_name="SubtitleJobV1",
                           state="failed", request_json={"job_id": str(job_id)}))
        db.commit()

    monkeypatch.setattr(sync, "get_sessionmaker", lambda url: factory)
    assert sync.reconcile_failed_child_subtitle_jobs(_settings()) == 1
    with factory() as db:
        job = db.get(SubtitleJob, job_id)
        assert job.status == SubtitleJobStatus.failed
        assert "Hatchet SubtitleJobV1 failed" in job.error_message
    assert sync.reconcile_failed_child_subtitle_jobs(_settings()) == 0
    engine.dispose()


def test_failed_hatchet_child_reconciles_queued_job_even_when_task_is_not_failed(monkeypatch):
    from videoroll.db.models import SubtitleJobStatus

    engine, factory, _ = _factory()
    with factory() as db:
        task = Task(
            source_type=SourceType.youtube,
            source_license=SourceLicense.authorized,
            status=TaskStatus.asr_done,
        )
        db.add(task)
        db.flush()
        failed_job = SubtitleJob(
            task_id=task.id,
            status=SubtitleJobStatus.queued,
            request_json={"runtime_profile": True},
            progress=0,
        )
        db.add(failed_job)
        db.flush()
        job_id = failed_job.id
        db.add(
            PipelineRun(
                task_id=task.id,
                engine="hatchet",
                workflow_name="SubtitleJobV1",
                state="failed",
                request_json={"job_id": str(job_id)},
            )
        )
        db.commit()

    monkeypatch.setattr(sync, "get_sessionmaker", lambda _url: factory)
    assert sync.reconcile_failed_child_subtitle_jobs(_settings()) == 1
    with factory() as db:
        job = db.get(SubtitleJob, job_id)
        task = db.get(Task, job.task_id)
        assert job.status == SubtitleJobStatus.failed
        assert task.status == TaskStatus.asr_done
    engine.dispose()
