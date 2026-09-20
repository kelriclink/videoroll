from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator

from redis import Redis
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)
_LOCAL_LOCK = threading.Lock()
_LOCAL_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_LOCAL_COOLDOWNS: dict[str, float] = {}


class ProviderGateTimeout(TimeoutError):
    """The caller could not enter a provider gate before its wait budget expired."""


def _set_local_cooldown(provider: str, target: float) -> None:
    with _LOCAL_LOCK:
        _LOCAL_COOLDOWNS[provider] = max(float(target), float(_LOCAL_COOLDOWNS.get(provider, 0.0)))


def _local_cooldown_until(provider: str) -> float:
    with _LOCAL_LOCK:
        return float(_LOCAL_COOLDOWNS.get(provider, 0.0))


def _local_semaphore(provider: str, max_concurrency: int) -> threading.BoundedSemaphore:
    key = (provider, max_concurrency)
    with _LOCAL_LOCK:
        semaphore = _LOCAL_SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(max_concurrency)
            _LOCAL_SEMAPHORES[key] = semaphore
        return semaphore



class ProviderRateGate:
    """Best-effort concurrency/cooldown guard for remote providers.

    Redis availability is deliberately not a correctness dependency: if Redis
    is unavailable a process-local semaphore still bounds concurrency and the
    provider request's own HTTP retry policy remains authoritative. When Redis
    is healthy, workers additionally share concurrency, Retry-After cooldowns,
    short-lived caches, and a small circuit-breaker state.
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
        self._local_semaphore = _local_semaphore(self.provider, self.max_concurrency)
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

    @property
    def _failure_key(self) -> str:
        return f"videoroll:provider-limit:{self.provider}:failures"

    @property
    def _circuit_key(self) -> str:
        return f"videoroll:provider-limit:{self.provider}:circuit-until"

    def _cache_key(self, key: str) -> str:
        digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()
        return f"videoroll:provider-limit:{self.provider}:cache:{digest}"

    def wait_for_cooldown(
        self,
        *,
        max_wait_seconds: float = 120.0,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
        while True:
            if cancel_check is not None:
                cancel_check()
            cooldown_until = _local_cooldown_until(self.provider)
            client = self._redis
            if client is not None:
                try:
                    raw = client.get(self._cooldown_key)
                    cooldown_until = max(cooldown_until, float(raw or 0.0))
                except (RedisError, OSError, ValueError, TypeError):
                    self._redis = None
            remaining = cooldown_until - time.time()
            if remaining <= 0:
                return
            remaining_wait = deadline - time.monotonic()
            if remaining_wait <= 0:
                raise ProviderGateTimeout(f"{self.provider} cooldown wait budget exceeded")
            time.sleep(min(0.1, remaining, remaining_wait))

    def set_cooldown(self, seconds: float) -> None:
        delay = max(0.0, min(300.0, float(seconds or 0.0)))
        if delay <= 0:
            return
        target = time.time() + delay
        _set_local_cooldown(self.provider, target)
        client = self._redis
        if client is None:
            return
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

    def is_circuit_open(self) -> bool:
        client = self._redis
        if client is None:
            return False
        try:
            return float(client.get(self._circuit_key) or 0.0) > time.time()
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None
            return False

    def record_success(self) -> None:
        client = self._redis
        if client is None:
            return
        try:
            client.delete(self._failure_key)
            client.delete(self._circuit_key)
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None

    def record_failure(self, *, threshold: int = 4, circuit_seconds: float = 30.0) -> None:
        client = self._redis
        if client is None:
            return
        try:
            count = int(client.incr(self._failure_key))
            client.expire(self._failure_key, 120)
            if count >= max(1, int(threshold)):
                delay = max(1.0, min(300.0, float(circuit_seconds)))
                client.set(self._circuit_key, f"{time.time() + delay:.6f}", ex=max(2, int(delay) + 2))
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None

    def cache_get(self, key: str) -> str | None:
        client = self._redis
        if client is None:
            return None
        try:
            value = client.get(self._cache_key(key))
            return str(value) if value is not None else None
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None
            return None

    def cache_set(self, key: str, value: str, *, ttl_seconds: float) -> None:
        client = self._redis
        if client is None:
            return
        ttl = max(1, min(86_400, int(float(ttl_seconds or 0.0))))
        try:
            client.set(self._cache_key(key), str(value), ex=ttl)
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None

    def _acquire_slot(
        self,
        *,
        wait_seconds: float = 120.0,
        cancel_check: Callable[[], None] | None = None,
    ) -> str | None:
        client = self._redis
        if client is None:
            return None
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            if cancel_check is not None:
                cancel_check()
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
                raise ProviderGateTimeout(f"{self.provider} distributed concurrency wait budget exceeded")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _release_slot(self, owner: str | None) -> None:
        client = self._redis
        if client is None or not owner:
            return
        try:
            client.zrem(self._slots_key, owner)
        except (RedisError, OSError, ValueError, TypeError):
            self._redis = None

    @contextlib.contextmanager
    def slot(
        self,
        *,
        wait_seconds: float = 120.0,
        cancel_check: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        wait_budget = max(0.0, float(wait_seconds))
        deadline = time.monotonic() + wait_budget
        if cancel_check is not None:
            cancel_check()
        if wait_budget <= 0:
            local_acquired = self._local_semaphore.acquire(blocking=False)
        else:
            local_acquired = False
            while not local_acquired:
                if cancel_check is not None:
                    cancel_check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                local_acquired = self._local_semaphore.acquire(timeout=min(0.1, remaining))
        if not local_acquired:
            raise ProviderGateTimeout(f"{self.provider} local concurrency wait budget exceeded")
        owner: str | None = None
        try:
            remaining = max(0.0, deadline - time.monotonic())
            self.wait_for_cooldown(max_wait_seconds=remaining, cancel_check=cancel_check)
            remaining = max(0.0, deadline - time.monotonic())
            owner = self._acquire_slot(wait_seconds=remaining, cancel_check=cancel_check)
            try:
                yield
            finally:
                self._release_slot(owner)
        finally:
            self._local_semaphore.release()
