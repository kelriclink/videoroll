"""Read-only Celery/Redis broker snapshot; never mutate queue or ACK messages."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from redis import Redis

QUEUE_NAMES = ("subtitle-control", "publish", "social_publish", "celery")
READY_SCAN_LIMIT = 3000
UNACKED_SCAN_LIMIT = 10000


def _task_queue(item: dict[str, Any]) -> str | None:
    info = item.get("delivery_info") or {}
    key = info.get("routing_key") or item.get("queue")
    return str(key) if key else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.replace(tzinfo=value.tzinfo or timezone.utc)
    except (ValueError, OverflowError):
        return None


def _queue_messages(redis_client: Any, name: str, now: datetime) -> dict[str, Any]:
    count = int(redis_client.llen(name))
    sampled = min(count, READY_SCAN_LIMIT)
    retry_count = 0
    unknown_stamp = False
    oldest: datetime | None = None
    for end in range(0, sampled, 500):
        rows = redis_client.lrange(name, end, min(end + 499, sampled - 1))
        for raw in rows:
            try:
                headers = json.loads(raw).get("headers") or {}
                retry_count += max(0, int(headers.get("retries") or 0))
                stamp = _parse_timestamp(headers.get("videoroll_enqueued_at"))
                if stamp is not None and (oldest is None or stamp < oldest):
                    oldest = stamp
                if stamp is None:
                    unknown_stamp = True
            except (TypeError, ValueError, AttributeError):
                unknown_stamp = True
    # Never report a misleading precise age for partially scanned, unstamped
    # or legacy queue contents. Empty queues have a well-defined zero wait.
    age = 0 if count == 0 else (
        max(0, int((now - oldest).total_seconds()))
        if oldest is not None and not unknown_stamp and sampled == count else None
    )
    return {
        "name": name,
        "ready": count,
        "reserved": None,
        "active": None,
        "unacked": 0,
        "oldest_wait_seconds": age,
        "ready_retry_attempts": retry_count,
        "sampled_ready": sampled,
        "scan_truncated": sampled < count,
    }


def collect_broker_snapshot(
    redis_client: Any,
    celery_app: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    queues = {name: _queue_messages(redis_client, name, now) for name in QUEUE_NAMES}
    warnings: list[str] = []

    # Kombu Redis transports keep unacknowledged deliveries in a hash and a
    # time-indexed sorted set. Each hash value holds message/exchange/routing.
    unacked_seen = 0
    if redis_client.type("unacked") == b"hash":
        cursor = 0
        while True:
            cursor, entries = redis_client.hscan("unacked", cursor=cursor, count=500)
            for value in entries.values():
                unacked_seen += 1
                try:
                    entry = json.loads(value)
                    key = entry[2] if isinstance(entry, list) and len(entry) >= 3 else None
                    if isinstance(key, str) and key in queues:
                        queues[key]["unacked"] += 1
                except (TypeError, ValueError, IndexError):
                    pass
                if unacked_seen >= UNACKED_SCAN_LIMIT:
                    warnings.append("unacked 扫描达到安全上限，统计可能不完整")
                    break
            if cursor == 0 or unacked_seen >= UNACKED_SCAN_LIMIT:
                break

    worker_ok = False
    try:
        inspector = celery_app.control.inspect(timeout=0.8)
        active = inspector.active()
        reserved = inspector.reserved()
        if isinstance(active, dict) and isinstance(reserved, dict) and active and reserved:
            worker_ok = True
            for queue in queues.values():
                queue["active"] = 0
                queue["reserved"] = 0
            for kind, responses in (("active", active), ("reserved", reserved)):
                for tasks in responses.values():
                    for task in tasks or []:
                        name = _task_queue(task)
                        if name in queues:
                            queues[name][kind] += 1
    except Exception:
        pass
    if not worker_ok:
        warnings.append("Worker 未完整响应状态检查，Active/Reserved 不代表零")
    if any(q["scan_truncated"] for q in queues.values()):
        warnings.append("等待消息扫描受限，重试次数仅统计已扫描部分")
    return {
        "observed_at": now.isoformat(),
        "worker_observed": worker_ok,
        "queues": list(queues.values()),
        "warnings": warnings,
    }


def read_broker_snapshot(redis_url: str) -> dict[str, Any]:
    # Celery control broadcasts only require a broker connection, not the
    # subtitle worker's heavyweight ASR/translation/render dependencies.
    from celery import Celery

    redis_client = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=3)
    monitor = Celery("videoroll-broker-telemetry", broker=redis_url)
    return collect_broker_snapshot(redis_client, monitor)
