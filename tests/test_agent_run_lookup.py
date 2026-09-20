from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from videoroll.apps.subtitle_service.rag import _reconcile_agent_run_status_from_steps, get_agent_run
from videoroll.apps.subtitle_service.schemas import AgentRunRead, AgentSkillRead


class _Result:
    def __init__(self, row: object | None) -> None:
        self.row = row

    def first(self) -> object | None:
        return self.row


class _Db:
    def __init__(self, row: object | None) -> None:
        self.row = row
        self.params: dict[str, str] | None = None

    def execute(self, _statement: object, params: dict[str, str]) -> _Result:
        self.params = params
        return _Result(self.row)


def test_get_agent_run_returns_one_full_run() -> None:
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    row = SimpleNamespace(
        _mapping={
            "id": run_id,
            "agent_type": "rag_term_research",
            "status": "running",
            "term": "AWP",
            "domain": "CS2",
            "target_lang": "zh",
            "task_id": None,
            "subtitle_job_id": None,
            "query": "AWP meaning",
            "steps": '[{"kind":"search"}]',
            "result": '{"knowledge_status":"pending"}',
            "error": "",
            "knowledge_item_id": None,
            "parent_agent_run_id": None,
            "started_at": now,
            "finished_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    db = _Db(row)

    result = get_agent_run(db, str(run_id))  # type: ignore[arg-type]

    assert db.params == {"id": str(run_id)}
    assert result is not None
    assert result["id"] == str(run_id)
    assert result["steps"] == [{"kind": "search"}]
    assert result["result"] == {"knowledge_status": "pending"}
    assert result["subtitle_job_status"] is None


def test_get_agent_run_returns_none_when_missing() -> None:
    assert get_agent_run(_Db(None), str(uuid.uuid4())) is None  # type: ignore[arg-type]


def test_get_agent_run_keeps_linked_subtitle_job_status() -> None:
    run_id = uuid.uuid4()
    job_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    row = SimpleNamespace(
        _mapping={
            "id": run_id,
            "agent_type": "subtitle_translation_session",
            "status": "running",
            "term": "字幕翻译 Session",
            "domain": "subtitle_translation",
            "target_lang": "zh",
            "task_id": uuid.uuid4(),
            "subtitle_job_id": job_id,
            "subtitle_job_status": "succeeded",
            "query": "20 segments",
            "steps": "[]",
            "result": "{}",
            "error": "",
            "knowledge_item_id": None,
            "parent_agent_run_id": None,
            "started_at": now,
            "finished_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )

    result = get_agent_run(_Db(row), str(run_id))  # type: ignore[arg-type]

    assert result is not None
    assert result["subtitle_job_id"] == str(job_id)
    assert result["subtitle_job_status"] == "succeeded"


def test_terminal_batch_event_repairs_stale_running_status() -> None:
    value = {
        "status": "running",
        "finished_at": None,
        "steps": [
            {
                "action": "translation_batch.started",
                "input": {
                    "batch_number": 7,
                    "segment_start": 301,
                    "segment_end": 350,
                    "segment_count": 50,
                },
            },
            {
                "action": "translation_batch.completed",
                "at": "2026-09-20T14:30:00+00:00",
                "metadata": {
                    "requested_segments": 50,
                    "translated_segments": 50,
                    "thought_characters": 1234,
                    "thought_truncated": False,
                },
                "output": {
                    "completed_segments": 350,
                    "updated_summary": "summary",
                },
            },
        ],
    }

    _reconcile_agent_run_status_from_steps(value)

    assert value["status"] == "succeeded"
    assert value["finished_at"] == "2026-09-20T14:30:00+00:00"
    assert value["result"]["batch_number"] == 7
    assert value["result"]["segment_start"] == 301
    assert value["result"]["segment_end"] == 350
    assert value["result"]["translated_segments"] == 50
    assert value["result"]["completed_segments"] == 350
    assert value["result"]["updated_summary"] == "summary"


def test_partial_batch_event_repairs_stale_running_status() -> None:
    value = {
        "status": "running",
        "finished_at": None,
        "steps": [
            {
                "action": "translation_batch.completed",
                "metadata": {"requested_segments": 50, "translated_segments": 17},
            }
        ],
    }

    _reconcile_agent_run_status_from_steps(value)

    assert value["status"] == "partial"


def test_terminal_session_failure_repairs_stale_running_status() -> None:
    value = {
        "status": "running",
        "finished_at": None,
        "steps": [
            {
                "action": "translation_session.started",
                "input": {"segment_count": 240, "resumed_segments": 50},
            },
            {
                "action": "translation_session.failed",
                "error": "provider failed",
                "output": {
                    "total_segments": 240,
                    "completed_segments": 150,
                    "batch_count": 4,
                    "succeeded_batches": 3,
                    "failed_batches": 1,
                    "thought_characters": 9000,
                },
            },
        ],
    }

    _reconcile_agent_run_status_from_steps(value)

    assert value["status"] == "failed"
    assert value["result"]["total_segments"] == 240
    assert value["result"]["completed_segments"] == 150
    assert value["result"]["resumed_segments"] == 50
    assert value["result"]["failed_batches"] == 1
    assert value["error"] == "provider failed"


def test_agent_run_schema_keeps_timestamps_without_requiring_them_for_skills() -> None:
    now = datetime.now(tz=timezone.utc)
    run = AgentRunRead(
        id=uuid.uuid4(),
        agent_type="rag_term_research",
        status="running",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    assert run.started_at == now
    assert AgentSkillRead(name="web-research").name == "web-research"
