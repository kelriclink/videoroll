from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from videoroll.apps.subtitle_service.rag import get_agent_run
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


def test_get_agent_run_returns_none_when_missing() -> None:
    assert get_agent_run(_Db(None), str(uuid.uuid4())) is None  # type: ignore[arg-type]


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
