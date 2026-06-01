from __future__ import annotations

import os
import sys
from typing import Any, Sequence

from celery.app.control import flatten_reply

from videoroll.apps.subtitle_service.render_queue_store import get_task_queue_settings
from videoroll.db.session import get_sessionmaker

_MIN_WORKER_CONCURRENCY = 1
_MAX_WORKER_CONCURRENCY = 32
_RUNTIME_CONTROL_TIMEOUT_SECONDS = 1.5
_SUBTITLE_QUEUE_NAME = "subtitle"


def normalize_subtitle_worker_concurrency(value: Any, *, fallback: int = 1) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(fallback)
    if n < _MIN_WORKER_CONCURRENCY:
        return _MIN_WORKER_CONCURRENCY
    if n > _MAX_WORKER_CONCURRENCY:
        return _MAX_WORKER_CONCURRENCY
    return n


def subtitle_worker_concurrency_for_task_queue_settings(settings: dict[str, Any], *, fallback: int = 1) -> int:
    if not isinstance(settings, dict):
        return normalize_subtitle_worker_concurrency(fallback, fallback=fallback)
    return normalize_subtitle_worker_concurrency(settings.get("max_concurrency"), fallback=fallback)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def subtitle_worker_destinations(
    celery_app: Any,
    *,
    queue: str = _SUBTITLE_QUEUE_NAME,
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> list[str]:
    inspector = celery_app.control.inspect(timeout=timeout)
    active_queues = inspector.active_queues() or {}

    hosts: list[str] = []
    for hostname, queue_infos in _as_dict(active_queues).items():
        host = str(hostname or "").strip()
        if not host:
            continue
        for queue_info in _as_list(queue_infos):
            if str(_as_dict(queue_info).get("name") or "").strip() == queue:
                hosts.append(host)
                break
    return sorted(set(hosts))


def _extract_worker_pool_concurrency(stats: dict[str, Any]) -> int | None:
    pool = _as_dict(stats.get("pool"))
    raw: Any = pool.get("max-concurrency")
    if raw is None:
        raw = pool.get("max_concurrency")
    if raw is None:
        processes = pool.get("processes")
        if isinstance(processes, list) and processes:
            raw = len(processes)
    if raw is None:
        return None
    return normalize_subtitle_worker_concurrency(raw)


def subtitle_worker_pool_concurrency(
    celery_app: Any,
    *,
    destinations: Sequence[str],
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, int]:
    if not destinations:
        return {}

    inspector = celery_app.control.inspect(destination=list(destinations), timeout=timeout)
    stats = inspector.stats() or {}

    resolved: dict[str, int] = {}
    for hostname, info in _as_dict(stats).items():
        host = str(hostname or "").strip()
        if not host:
            continue
        pool_concurrency = _extract_worker_pool_concurrency(_as_dict(info))
        if pool_concurrency is None:
            continue
        resolved[host] = pool_concurrency
    return resolved


def _flatten_control_reply(reply: Any) -> dict[str, Any]:
    if isinstance(reply, list):
        return flatten_reply(reply)
    return _as_dict(reply)


def _control_reply_status(reply: Any) -> tuple[bool, str | None]:
    data = _as_dict(reply)
    ok = data.get("ok")
    if ok not in (None, False, ""):
        return True, str(ok).strip() or "ok"

    error = data.get("error")
    if error not in (None, ""):
        return False, str(error).strip()

    nok = data.get("nok")
    if nok not in (None, ""):
        return False, str(nok).strip()

    if isinstance(reply, str) and reply.strip():
        return True, reply.strip()

    return False, None


def sync_subtitle_worker_concurrency(
    celery_app: Any,
    target_concurrency: Any,
    *,
    queue: str = _SUBTITLE_QUEUE_NAME,
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    target = normalize_subtitle_worker_concurrency(target_concurrency)
    result: dict[str, Any] = {
        "ok": False,
        "queue": queue,
        "target_concurrency": target,
        "detail": None,
        "workers": [],
    }

    try:
        destinations = subtitle_worker_destinations(celery_app, queue=queue, timeout=timeout)
    except Exception as e:
        result["detail"] = f"failed to inspect workers for queue '{queue}': {type(e).__name__}: {e}"
        return result

    if not destinations:
        result["detail"] = f"no running workers consume queue '{queue}'"
        return result

    try:
        current_by_host = subtitle_worker_pool_concurrency(celery_app, destinations=destinations, timeout=timeout)
    except Exception as e:
        result["detail"] = f"failed to inspect worker pool stats: {type(e).__name__}: {e}"
        return result

    workers: list[dict[str, Any]] = []
    failed_hosts: list[str] = []
    changed_hosts = 0

    for hostname in destinations:
        current = current_by_host.get(hostname)
        item: dict[str, Any] = {
            "hostname": hostname,
            "current_concurrency": current,
            "target_concurrency": target,
            "action": "noop",
            "ok": False,
            "detail": None,
        }
        if current is None:
            item["detail"] = "worker stats unavailable"
            failed_hosts.append(hostname)
            workers.append(item)
            continue

        delta = target - current
        if delta == 0:
            item["ok"] = True
            item["detail"] = "already at target"
            workers.append(item)
            continue

        try:
            if delta > 0:
                item["action"] = "grow"
                reply = celery_app.control.pool_grow(delta, destination=[hostname], reply=True, timeout=timeout)
            else:
                item["action"] = "shrink"
                reply = celery_app.control.pool_shrink(-delta, destination=[hostname], reply=True, timeout=timeout)
        except Exception as e:
            item["detail"] = f"{type(e).__name__}: {e}"
            failed_hosts.append(hostname)
            workers.append(item)
            continue

        reply_by_host = _flatten_control_reply(reply)
        ok, detail = _control_reply_status(reply_by_host.get(hostname))
        item["ok"] = ok
        if ok:
            changed_hosts += 1
            item["detail"] = detail or f"pool will {item['action']}"
        else:
            item["detail"] = detail or f"worker did not acknowledge pool_{item['action']}"
            failed_hosts.append(hostname)
        workers.append(item)

    result["workers"] = workers
    result["ok"] = not failed_hosts and bool(workers)
    if failed_hosts:
        result["detail"] = f"sync incomplete for {len(failed_hosts)}/{len(workers)} worker(s): {', '.join(failed_hosts)}"
    elif changed_hosts > 0:
        result["detail"] = f"synchronized {len(workers)} worker(s) to concurrency={target}"
    else:
        result["detail"] = f"worker concurrency already at {target}"
    return result


def sync_subtitle_worker_concurrency_for_task_queue_settings(
    celery_app: Any,
    settings: dict[str, Any],
    *,
    queue: str = _SUBTITLE_QUEUE_NAME,
    timeout: float = _RUNTIME_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    target = subtitle_worker_concurrency_for_task_queue_settings(settings)
    return sync_subtitle_worker_concurrency(celery_app, target, queue=queue, timeout=timeout)


def resolve_subtitle_worker_concurrency(database_url: str, *, fallback: int = 1) -> int:
    db_url = str(database_url or "").strip()
    if not db_url:
        return normalize_subtitle_worker_concurrency(fallback, fallback=fallback)

    SessionLocal = get_sessionmaker(db_url)
    db = SessionLocal()
    try:
        cfg = get_task_queue_settings(db)
        return subtitle_worker_concurrency_for_task_queue_settings(cfg, fallback=fallback)
    finally:
        db.close()


def main() -> int:
    fallback = normalize_subtitle_worker_concurrency(os.getenv("CELERY_SUB_CONCURRENCY_FALLBACK", "1"))
    try:
        resolved = resolve_subtitle_worker_concurrency(os.getenv("DATABASE_URL", ""), fallback=fallback)
    except Exception as e:
        print(f"warning: failed to resolve subtitle worker concurrency from task queue settings: {e}", file=sys.stderr)
        resolved = fallback
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
