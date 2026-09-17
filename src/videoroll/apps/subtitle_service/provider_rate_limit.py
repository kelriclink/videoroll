from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Iterator

from redis import Redis
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)


class ProviderRateGate:
    """Best-effort distributed concurrency/cooldown guard for remote ASR APIs.

    Redis availability is deliberately not a correctness dependency: if Redis
    is unavailable the provider request still runs and its own HTTP retry policy
    remains authoritative.  When Redis is healthy, workers share both a bounded
    concurrency semaphore and Retry-After cooldowns so they do not form a retry
    convoy after a provider 429.
    """

    def __init__(
        self,
        redis_url: str,
        provider: str,
        *,
        max_concurrency: int = 1,
        slot_ttl_seconds: float = 300.0,
    ) -> None:
        self.provider = str(provider or "provider").strip().lower().replace(" ", "-")[:64] or "provider"
        self.max_concurrency = max(1, min(32, int(max_concurrency or 1)))
        self.slot_ttl_seconds = max(30.0, min(3600.0, float(slot_ttl_seconds)))
        self._redis: Redis | None = None
        if str(redis_url or "").strip():
            try:
                self._redis = Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
            except (RedisError, OSError, ValueError, TypeError):
                self._redis = None

    @property
    def _slots_key(self) -> str:
        return f"videoroll:provider-limit:{self.provider}:slots"

    @property
    def _cooldown_key(self) -> str:
        return f"videoroll:provider-limit:{self.provider}:cooldown-until"

    def wait_for_cooldown(self, *, max_wait_seconds: float = 120.0) -> None:
        client = self._redis
        if client is None:
            return
        deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
        while True:
            try:
                raw = client.get(self._cooldown_key)
                cooldown_until = float(raw or 0.0)
            except (RedisError, OSError, ValueError, TypeError):
                self._redis = None
                return
            remaining = cooldown_until - time.time()
            if remaining <= 0:
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(min(1.0, remaining, max(0.0, deadline - time.monotonic())))

    def set_cooldown(self, seconds: float) -> None:
        client = self._redis
        delay = max(0.0, min(300.0, float(seconds or 0.0)))
        if client is None or delay <= 0:
            return
        target = time.time() + delay
        ttl = max(1, int(delay) + 5)
        # Keep the longest concurrently announced Retry-After. A GET+SET pair
        # can race and let a shorter cooldown overwrite a longer one, so perform
        # the max update atomically in Redis.
        script = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local target = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
if target > current then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
  return 1
end
return 0
"""
        try:
            client.eval(script, 1, self._cooldown_key, f"{target:.6f}", str(ttl))
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None

    def _acquire_slot(self, *, wait_seconds: float = 120.0) -> str | None:
        client = self._redis
        if client is None:
            return None
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            now = time.time()
            try:
                pipe = client.pipeline(transaction=True)
                pipe.zremrangebyscore(self._slots_key, "-inf", now)
                pipe.zadd(self._slots_key, {owner: now + self.slot_ttl_seconds}, nx=True)
                pipe.zrank(self._slots_key, owner)
                pipe.expire(self._slots_key, max(60, int(self.slot_ttl_seconds) + 30))
                results = pipe.execute()
                rank = results[2]
                if rank is not None and int(rank) < self.max_concurrency:
                    return owner
                client.zrem(self._slots_key, owner)
            except (RedisError, OSError, ValueError, TypeError):
                self._redis = None
                return None
            if time.monotonic() >= deadline:
                # Fail open after a bounded wait. Provider HTTP limits remain
                # the final source of truth and still receive normal retries.
                return None
            time.sleep(0.25)

    def _release_slot(self, owner: str | None) -> None:
        client = self._redis
        if client is None or not owner:
            return
        try:
            client.zrem(self._slots_key, owner)
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None

    @contextlib.contextmanager
    def slot(self, *, wait_seconds: float = 120.0) -> Iterator[None]:
        self.wait_for_cooldown(max_wait_seconds=wait_seconds)
        owner = self._acquire_slot(wait_seconds=wait_seconds)
        try:
            yield
        finally:
            self._release_slot(owner)
