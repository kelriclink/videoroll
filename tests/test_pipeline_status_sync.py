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
from videoroll.db.models import PipelineRun, SourceLicense, SourceType, Task, TaskStatus
from videoroll.workflows import pipeline_status_sync as sync


@compiles(JSONB, 'sqlite')
def _sqlite_jsonb(_type, _compiler, **kwargs):
    return 'JSON'


def _factory():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Task.__table__, PipelineRun.__table__])
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
    assert result == {'checked': 4, 'updated': 4, 'errors': 0}
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
