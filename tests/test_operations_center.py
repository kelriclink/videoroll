from __future__ import annotations

import asyncio
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from videoroll.ai.client import (
    OpenAIChatConfig,
    OpenAIRequestError,
    request_openai_json_object,
    request_openai_json_object_with_thinking,
)
from videoroll.apps.orchestrator_api.services import operations_service
from videoroll.apps.subtitle_service import main as subtitle_main
from videoroll.apps.subtitle_service.main import _parse_knowledge_import, _read_task_queue
from videoroll.apps.subtitle_service.rag import RagSettings, rebuild_knowledge_embeddings
from videoroll.db.base import Base
from videoroll.db.models import (
    AIUsageEvent,
    Account,
    AlertEvent,
    AppSetting,
    OperationInbox,
    RenderExecution,
    RenderJob,
    RenderJobStatus,
    RenderWorker,
    SourceLicense,
    SourceType,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)

from videoroll.utils.auto_youtube import encode_auto_youtube_created_by


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def _mock_healthy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        operations_service.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10, free=90),
    )


@pytest.fixture
def operations_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    tables = [
        Task.__table__,
        AppSetting.__table__,
        OperationInbox.__table__,
        Account.__table__,
        SubtitleJob.__table__,
        RenderJob.__table__,
        RenderWorker.__table__,
        RenderExecution.__table__,
        AIUsageEvent.__table__,
        AlertEvent.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine, tables=list(reversed(tables)))
        engine.dispose()


def test_task_queue_projects_hatchet_priority_without_legacy_position_ordering(operations_db: Session) -> None:
    low = Task(
        source_type=SourceType.local,
        source_license=SourceLicense.own,
        status=TaskStatus.downloaded,
        priority=0,
    )
    high_late = Task(
        source_type=SourceType.local,
        source_license=SourceLicense.own,
        status=TaskStatus.downloaded,
        priority=50,
    )
    high_first = Task(
        source_type=SourceType.local,
        source_license=SourceLicense.own,
        status=TaskStatus.downloaded,
        priority=50,
    )
    operations_db.add_all([low, high_late, high_first])
    operations_db.flush()
    for task in (low, high_late, high_first):
        operations_db.add(SubtitleJob(task_id=task.id, status=SubtitleJobStatus.queued, request_json={}))
    operations_db.commit()

    queue = _read_task_queue(operations_db, limit=20)
    ids = [item.task_id for item in queue.tasks if item.state == "queued"]

    # Hatchet priority is authoritative. Tasks in the same priority bucket
    # intentionally have no user-controlled secondary queue position.
    assert set(ids[:2]) == {high_late.id, high_first.id}
    assert ids[2] == low.id
    assert all(item.priority == 50 for item in queue.tasks[:2])



def test_task_queue_reports_real_youtube_download_progress(
    monkeypatch: pytest.MonkeyPatch,
    operations_db: Session,
) -> None:
    task = Task(
        source_type=SourceType.youtube,
        source_url="https://www.youtube.com/watch?v=demo",
        source_license=SourceLicense.own,
        status=TaskStatus.ingested,
        created_by=encode_auto_youtube_created_by("auto_youtube", auto_publish=None, run_id="queue-progress"),
    )
    operations_db.add(task)
    operations_db.commit()

    from videoroll.apps.orchestrator_api.services import youtube_service

    monkeypatch.setattr(
        youtube_service,
        "get_download_progress",
        lambda *_args, **_kwargs: {
            "status": "downloading",
            "active": True,
            "progress": 55,
            "error": None,
        },
    )

    queue = _read_task_queue(operations_db, limit=20)
    item = next(row for row in queue.tasks if row.task_id == task.id)

    assert item.state == "running"
    assert item.stage == "youtube_download"
    assert item.progress == 55


def test_task_queue_reports_pipeline_bootstrap_after_youtube_download(
    monkeypatch: pytest.MonkeyPatch,
    operations_db: Session,
) -> None:
    task = Task(
        source_type=SourceType.youtube,
        source_url="https://www.youtube.com/watch?v=demo",
        source_license=SourceLicense.own,
        status=TaskStatus.downloaded,
        created_by=encode_auto_youtube_created_by("auto_youtube", auto_publish=None, run_id="queue-complete"),
    )
    operations_db.add(task)
    operations_db.commit()

    from videoroll.apps.orchestrator_api.services import youtube_service

    monkeypatch.setattr(
        youtube_service,
        "get_download_progress",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "active": False,
            "progress": 100,
            "error": None,
        },
    )

    queue = _read_task_queue(operations_db, limit=20)
    item = next(row for row in queue.tasks if row.task_id == task.id)

    assert item.stage == "subtitle_handoff"
    assert item.progress == 100


def test_knowledge_bulk_parser_supports_csv_json_and_srt() -> None:
    csv_rows = _parse_knowledge_import(
        filename="terms.csv",
        raw=b"term,translation,aliases\nCPU,processor,chip|core\n",
        import_format="auto",
        target_lang="zh",
        domain="tech",
    )
    assert csv_rows[0]["term"] == "CPU"
    assert csv_rows[0]["aliases"] == ["chip", "core"]

    json_rows = _parse_knowledge_import(
        filename="items.json",
        raw=json.dumps({"items": [{"item_type": "term", "term": "GPU", "translation": "graphics"}]}).encode(),
        import_format="auto",
        target_lang="zh",
        domain="tech",
    )
    assert json_rows[0]["term"] == "GPU"

    srt_rows = _parse_knowledge_import(
        filename="sample.srt",
        raw="1\n00:00:00,000 --> 00:00:01,000\nHello\n\n2\n00:00:01,000 --> 00:00:02,000\nWorld\n".encode(),
        import_format="auto",
        target_lang="zh",
        domain="subtitle",
    )
    assert len(srt_rows) == 1
    assert srt_rows[0]["item_type"] == "document"
    assert "Hello" in srt_rows[0]["content"]
    assert "World" in srt_rows[0]["content"]


def test_knowledge_bulk_import_never_dedupes_terms_across_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(subtitle_main, "get_translate_settings", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        subtitle_main,
        "rag_settings_from_translate_settings",
        lambda _cfg: SimpleNamespace(
            embedding_provider="test",
            embedding_model="embedding",
            embedding_dimensions=2,
        ),
    )
    monkeypatch.setattr(subtitle_main, "embedding_settings_from_translate_settings", lambda _cfg: object())
    monkeypatch.setattr(subtitle_main, "embed_text", lambda *_args, **_kwargs: [0.1, 0.2])
    monkeypatch.setattr(subtitle_main, "assert_embedding_dimensions", lambda *_args, **_kwargs: None)

    def fake_upsert(_db, **kwargs):
        captured.append(kwargs)
        return str(uuid.uuid4())

    monkeypatch.setattr(subtitle_main, "upsert_knowledge_item", fake_upsert)

    class FakeDB:
        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    upload = UploadFile(
        filename="terms.csv",
        file=io.BytesIO(b"term,translation\nCPU,processor\n"),
    )
    result = asyncio.run(
        subtitle_main.import_knowledge_items_view(
            file=upload,
            import_format="csv",
            target_lang="zh",
            domain="hardware",
            settings=SimpleNamespace(),
            db=FakeDB(),  # type: ignore[arg-type]
        )
    )

    assert result.imported == 1
    assert captured[0]["domain"] == "hardware"
    assert captured[0]["dedupe_any_domain"] is False


def test_embedding_rebuild_prioritizes_never_or_oldest_verified_items() -> None:
    class EmptyRows:
        def all(self):
            return []

    class CaptureDB:
        sql = ""

        def execute(self, statement, _params):
            self.sql = str(statement)
            return EmptyRows()

    db = CaptureDB()
    result = rebuild_knowledge_embeddings(
        db,  # type: ignore[arg-type]
        rag_settings=RagSettings(embedding_provider="test", embedding_model="embed"),
        embedding_settings=object(),  # type: ignore[arg-type]
        limit=10000,
    )

    assert "CASE WHEN last_verified_at IS NULL THEN 0 ELSE 1 END" in db.sql
    assert "last_verified_at ASC" in db.sql
    assert "updated_at DESC" not in db.sql
    assert result["total"] == 0


def test_ai_usage_summary_and_pricing(operations_db: Session) -> None:
    operations_service.set_ai_pricing(
        operations_db,
        {
            "models": {
                "example.invalid:test-model": {
                    "input_per_million_usd": 1.0,
                    "output_per_million_usd": 2.0,
                }
            }
        },
    )
    now = datetime.now(timezone.utc)
    operations_db.add_all(
        [
            AIUsageEvent(
                provider="example.invalid",
                model="test-model",
                operation="translate",
                success=True,
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                estimated_cost_microusd=None,
                created_at=now,
            ),
            AIUsageEvent(
                provider="example.invalid",
                model="test-model",
                operation="translate",
                success=False,
                status_code=402,
                error_type="OpenAIRequestError",
                error_message="payment required",
                created_at=now,
            ),
        ]
    )
    operations_db.commit()

    summary = operations_service.ai_usage_summary(operations_db, hours=24)
    assert summary["requests"] == 2
    assert summary["successes"] == 1
    assert summary["failures"] == 1
    assert summary["total_tokens"] == 150
    assert summary["estimated_cost_usd"] == 0.0002
    assert summary["by_status"] == [{"status_code": 402, "count": 1}]
    assert operations_service.get_ai_pricing(operations_db)["models"]["example.invalid:test-model"]["input_per_million_usd"] == 1.0


def test_ai_pricing_rejects_non_finite_values(operations_db: Session) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        operations_service.set_ai_pricing(
            operations_db,
            {
                "models": {
                    "provider:model": {
                        "input_per_million_usd": float("nan"),
                        "output_per_million_usd": 1.0,
                    }
                }
            },
        )


def test_alert_scan_opens_and_resolves_ai_402(monkeypatch: pytest.MonkeyPatch, operations_db: Session, tmp_path) -> None:
    _mock_healthy_disk(monkeypatch)
    operations_db.add(
        AIUsageEvent(
            provider="provider",
            model="model",
            operation="translate",
            success=False,
            status_code=402,
            created_at=datetime.now(timezone.utc),
        )
    )
    operations_db.commit()

    settings = SimpleNamespace(storage_root=str(tmp_path))
    first = operations_service.scan_alerts(settings, operations_db)  # type: ignore[arg-type]
    assert first["active"] == 1
    alert = operations_db.query(AlertEvent).filter(AlertEvent.fingerprint == "ai:http:402").one()
    assert alert.status == "open"
    assert alert.severity == "critical"

    for row in operations_db.query(AIUsageEvent).all():
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    operations_db.commit()
    second = operations_service.scan_alerts(settings, operations_db)  # type: ignore[arg-type]
    assert second["active"] == 0
    operations_db.refresh(alert)
    assert alert.status == "resolved"


def test_alert_scan_opens_transport_error_after_threshold(monkeypatch: pytest.MonkeyPatch, operations_db: Session, tmp_path) -> None:
    _mock_healthy_disk(monkeypatch)
    now = datetime.now(timezone.utc)
    operations_db.add_all(
        [
            AIUsageEvent(
                provider="provider",
                model="model",
                operation="translate",
                success=False,
                status_code=None,
                error_type="ConnectError",
                error_message="connection failed",
                created_at=now,
            )
            for _ in range(3)
        ]
    )
    operations_db.commit()

    settings = SimpleNamespace(storage_root=str(tmp_path))
    result = operations_service.scan_alerts(settings, operations_db)  # type: ignore[arg-type]
    assert result["active"] == 1
    alert = operations_db.query(AlertEvent).filter(AlertEvent.fingerprint == "ai:http:transport").one()
    assert alert.status == "open"
    assert alert.severity == "warning"


def test_disk_probe_failure_keeps_capacity_alert_open_and_adds_probe_alert(
    monkeypatch: pytest.MonkeyPatch,
    operations_db: Session,
    tmp_path,
) -> None:
    settings = SimpleNamespace(storage_root=str(tmp_path))
    monkeypatch.setattr(
        operations_service.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=96, free=4),
    )
    operations_service.scan_alerts(settings, operations_db)  # type: ignore[arg-type]
    capacity = operations_db.query(AlertEvent).filter(AlertEvent.fingerprint == "disk:storage").one()
    assert capacity.status == "open"

    def fail_probe(_path):
        raise OSError("storage unavailable")

    monkeypatch.setattr(operations_service.shutil, "disk_usage", fail_probe)
    result = operations_service.scan_alerts(settings, operations_db)  # type: ignore[arg-type]
    operations_db.refresh(capacity)
    probe = operations_db.query(AlertEvent).filter(AlertEvent.fingerprint == "disk:storage:probe").one()

    assert capacity.status == "open"
    assert probe.status == "open"
    assert probe.severity == "critical"
    assert result["active"] == 2


def test_active_alert_listing_cannot_be_hidden_by_newer_resolved_history(operations_db: Session) -> None:
    now = datetime.now(timezone.utc)
    active = AlertEvent(
        fingerprint="queue:stale:subtitle:old",
        source="queue",
        severity="critical",
        status="open",
        title="still open",
        message="",
        details_json={},
        first_seen_at=now - timedelta(days=2),
        last_seen_at=now - timedelta(days=2),
    )
    operations_db.add(active)
    operations_db.add_all(
        [
            AlertEvent(
                fingerprint=f"resolved:{index}",
                source="test",
                severity="info",
                status="resolved",
                title=f"resolved {index}",
                message="",
                details_json={},
                first_seen_at=now,
                last_seen_at=now,
                resolved_at=now,
            )
            for index in range(250)
        ]
    )
    operations_db.commit()

    rows = operations_service.list_alerts(operations_db, status="active", limit=200)

    assert [row["fingerprint"] for row in rows] == ["queue:stale:subtitle:old"]


def test_alert_upsert_reuses_fingerprint_and_increments_occurrence(operations_db: Session) -> None:
    first = operations_service.upsert_alert(
        operations_db,
        fingerprint="ai:http:429",
        source="ai",
        severity="warning",
        title="busy",
    )
    operations_db.commit()
    second = operations_service.upsert_alert(
        operations_db,
        fingerprint="ai:http:429",
        source="ai",
        severity="warning",
        title="busy again",
    )
    operations_db.commit()

    assert first.id == second.id
    assert operations_db.query(AlertEvent).filter(AlertEvent.fingerprint == "ai:http:429").count() == 1
    assert second.occurrence_count == 2


def test_resolving_unknown_external_alert_does_not_create_history(operations_db: Session) -> None:
    result = operations_service.report_alert(
        operations_db,
        {
            "fingerprint": "playout:rtmp:1",
            "source": "playout",
            "severity": "critical",
            "title": "RTMP 输出已恢复",
            "message": "recovered",
            "details": {"channel": 1, "target": "rtmp://example.invalid"},
            "resolved": True,
        },
    )

    assert result is None
    assert operations_db.query(AlertEvent).count() == 0


def test_direct_rtmp_alert_is_not_auto_resolved_by_periodic_scan(monkeypatch: pytest.MonkeyPatch, operations_db: Session, tmp_path) -> None:
    _mock_healthy_disk(monkeypatch)
    opened = operations_service.report_alert(
        operations_db,
        {
            "fingerprint": "playout:rtmp:1",
            "source": "playout",
            "severity": "critical",
            "title": "RTMP 输出断流",
            "message": "failed",
            "details": {"channel": 1, "target": "rtmp://example.invalid"},
            "resolved": False,
        },
    )
    assert opened is not None
    assert opened.status == "open"

    settings = SimpleNamespace(storage_root=str(tmp_path))
    operations_service.scan_alerts(settings, operations_db)  # type: ignore[arg-type]
    operations_db.refresh(opened)
    assert opened.status == "open"

    resolved = operations_service.report_alert(
        operations_db,
        {
            "fingerprint": "playout:rtmp:1",
            "source": "playout",
            "severity": "critical",
            "title": "RTMP 输出已恢复",
            "message": "recovered",
            "details": {"channel": 1, "target": "rtmp://example.invalid"},
            "resolved": True,
        },
    )
    assert resolved is not None
    assert resolved.status == "resolved"


def test_thinking_http_failure_records_one_terminal_event(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []
    monkeypatch.setattr("videoroll.ai.client.record_ai_usage", lambda **kwargs: events.append(kwargs))
    config = OpenAIChatConfig(api_key="test", base_url="https://example.invalid/v1", model="think-model")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            json={"error": {"message": "busy"}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenAIRequestError):
            request_openai_json_object_with_thinking(
                config=config,
                system_prompt="system",
                user_prompt="user",
                client=client,
                format_retries=1,
                network_retries=1,
            )

    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["status_code"] == 429
    assert int(events[0]["latency_ms"]) >= 0


def test_openai_client_emits_usage_and_http_error_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []
    monkeypatch.setattr("videoroll.ai.client.record_ai_usage", lambda **kwargs: events.append(kwargs))
    config = OpenAIChatConfig(api_key="test", base_url="https://example.invalid/v1", model="test-model")

    def ok_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(ok_handler)) as client:
        assert request_openai_json_object(
            config=config,
            system_prompt="system",
            user_prompt="user",
            client=client,
            format_retries=1,
            network_retries=1,
        ) == {"ok": True}

    assert events[-1]["success"] is True
    assert events[-1]["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    def fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "payment required"}})

    with httpx.Client(transport=httpx.MockTransport(fail_handler)) as client:
        with pytest.raises(OpenAIRequestError) as raised:
            request_openai_json_object(
                config=config,
                system_prompt="system",
                user_prompt="user",
                client=client,
                format_retries=1,
                network_retries=1,
            )

    assert raised.value.status_code == 402
    assert events[-1]["success"] is False
    assert events[-1]["status_code"] == 402


def test_openai_retries_record_every_http_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []
    attempts = 0
    monkeypatch.setattr("videoroll.ai.client.record_ai_usage", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr("videoroll.ai.client._sleep_before_retry", lambda *_args, **_kwargs: None)
    config = OpenAIChatConfig(api_key="test", base_url="https://example.invalid/v1", model="retry-model")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, json={"error": {"message": "busy"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_openai_json_object(
            config=config,
            system_prompt="system",
            user_prompt="user",
            client=client,
            format_retries=1,
            network_retries=3,
        )

    assert result == {"ok": True}
    assert [event["status_code"] for event in events] == [429, 429, 200]
    assert [event["success"] for event in events] == [False, False, True]


def test_thinking_retries_record_failed_attempt_before_success(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []
    attempts = 0
    monkeypatch.setattr("videoroll.ai.client.record_ai_usage", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr("videoroll.ai.client._sleep_before_retry", lambda *_args, **_kwargs: None)
    config = OpenAIChatConfig(api_key="test", base_url="https://example.invalid/v1", model="think-retry")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_openai_json_object_with_thinking(
            config=config,
            system_prompt="system",
            user_prompt="user",
            client=client,
            format_retries=1,
            network_retries=2,
        )

    assert result == {"ok": True}
    assert [event["status_code"] for event in events] == [500, 200]
    assert [event["success"] for event in events] == [False, True]
