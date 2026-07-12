from __future__ import annotations

from functools import lru_cache

from celery import Celery


@lru_cache
def get_publish_cleanup_sender(redis_url: str) -> Celery:
    """Create the small, configured sender used outside Celery workers.

    The worker owns task execution.  API-side publication only needs a broker
    client, so it must not import a worker module merely to call ``send_task``.
    """
    return Celery("publish_cleanup_dispatch", broker=redis_url, backend=redis_url)
