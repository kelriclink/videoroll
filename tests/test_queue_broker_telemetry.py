from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from videoroll.apps.subtitle_service.queue_telemetry import collect_broker_snapshot


class FakeRedis:
    def __init__(self, messages: dict[str, list[dict[str, object]]], unacked: dict[str, bytes] | None = None):
        self.messages = {k: [json.dumps(v).encode() for v in rows] for k, rows in messages.items()}
        self.unacked = unacked or {}

    def llen(self, name: str) -> int:
        return len(self.messages.get(name, []))

    def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        return self.messages.get(name, [])[start:end + 1]

    def type(self, name: str) -> bytes:
        return b"hash" if name == "unacked" and self.unacked else b"none"

    def hscan(self, name: str, cursor: int, count: int) -> tuple[int, dict[str, bytes]]:
        return 0, self.unacked


def test_broker_snapshot_reports_distinct_ready_active_reserved_unacked_and_retries() -> None:
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    stamp = (now - timedelta(minutes=10)).isoformat()
    redis = FakeRedis(
        {"publish": [{"headers": {"videoroll_enqueued_at": stamp, "retries": 2}}]},
        {"tag-1": json.dumps([{}, "", "publish"]).encode()},
    )
    inspect = MagicMock()
    inspect.active.return_value = {"bilibili@host": [{"delivery_info": {"routing_key": "publish"}}]}
    inspect.reserved.return_value = {"bilibili@host": [{"delivery_info": {"routing_key": "publish"}}]}
    app = MagicMock()
    app.control.inspect.return_value = inspect

    result = collect_broker_snapshot(redis, app, now=now)
    publish = next(row for row in result["queues"] if row["name"] == "publish")
    assert result["worker_observed"] is True
    assert (publish["ready"], publish["reserved"], publish["active"], publish["unacked"]) == (1, 1, 1, 1)
    assert publish["oldest_wait_seconds"] == 600
    assert publish["ready_retry_attempts"] == 2


def test_broker_snapshot_does_not_invent_age_or_worker_state_when_missing() -> None:
    app = MagicMock()
    app.control.inspect.return_value.active.return_value = None
    app.control.inspect.return_value.reserved.return_value = None
    result = collect_broker_snapshot(FakeRedis({"celery": [{"headers": {"retries": 0}}]}), app)
    queue = next(row for row in result["queues"] if row["name"] == "celery")
    assert queue["ready"] == 1
    assert queue["oldest_wait_seconds"] is None
    assert queue["active"] is None
    assert queue["reserved"] is None
    assert result["warnings"]


def test_empty_queue_has_zero_wait_age() -> None:
    app = MagicMock()
    app.control.inspect.return_value.active.return_value = {"worker": []}
    app.control.inspect.return_value.reserved.return_value = {"worker": []}
    result = collect_broker_snapshot(FakeRedis({}), app)
    assert all(q["oldest_wait_seconds"] == 0 for q in result["queues"])
