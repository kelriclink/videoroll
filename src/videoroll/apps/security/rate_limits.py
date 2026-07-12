from __future__ import annotations

from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError


LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60
_KEY_PREFIX = "videoroll:auth-rate-limit:"

_RECORD_FAILURE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0
    attempts: int = 0


class RateLimitUnavailable(RuntimeError):
    pass


def _client(redis_url: str) -> Redis:
    return Redis.from_url(
        redis_url,
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _redis_key(key: str) -> str:
    normalized = str(key or "").strip().lower()
    if not normalized or len(normalized) > 192:
        raise ValueError("invalid rate-limit key")
    return _KEY_PREFIX + normalized


def _retry_after(ttl: int) -> int:
    return ttl if ttl > 0 else LOGIN_WINDOW_SECONDS


def check_login_rate_limit(redis_url: str, key: str) -> RateLimitDecision:
    redis_key = _redis_key(key)
    try:
        client = _client(redis_url)
        raw_count = client.get(redis_key)
        count = int(raw_count or 0)
        if count < LOGIN_FAILURE_LIMIT:
            return RateLimitDecision(allowed=True, attempts=count)
        ttl = int(client.ttl(redis_key))
        return RateLimitDecision(
            allowed=False,
            retry_after=_retry_after(ttl),
            attempts=count,
        )
    except (RedisError, OSError, ValueError) as exc:
        raise RateLimitUnavailable("login rate limiter unavailable") from exc


def record_login_failure(redis_url: str, key: str) -> RateLimitDecision:
    redis_key = _redis_key(key)
    try:
        count, ttl = _client(redis_url).eval(
            _RECORD_FAILURE_SCRIPT,
            1,
            redis_key,
            LOGIN_WINDOW_SECONDS,
        )
        count_i = int(count)
        return RateLimitDecision(
            allowed=count_i < LOGIN_FAILURE_LIMIT,
            retry_after=0 if count_i < LOGIN_FAILURE_LIMIT else _retry_after(int(ttl)),
            attempts=count_i,
        )
    except (RedisError, OSError, ValueError, TypeError) as exc:
        raise RateLimitUnavailable("login rate limiter unavailable") from exc


def clear_login_rate_limit(redis_url: str, key: str) -> None:
    try:
        _client(redis_url).delete(_redis_key(key))
    except (RedisError, OSError, ValueError) as exc:
        raise RateLimitUnavailable("login rate limiter unavailable") from exc
