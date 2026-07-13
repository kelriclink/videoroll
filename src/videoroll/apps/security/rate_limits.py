from __future__ import annotations

from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError


LOGIN_FAILURE_LIMIT = 5
LOGIN_BURST_WINDOW_SECONDS = 60
LOGIN_LOCKOUT_BASE_SECONDS = 120
LOGIN_LOCKOUT_MAX_SECONDS = 60 * 60
_KEY_PREFIX = "videoroll:auth-rate-limit:"

_RECORD_FAILURE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
local limit = tonumber(ARGV[1])
local burst_window = tonumber(ARGV[2])
local lockout_base = tonumber(ARGV[3])
local lockout_max = tonumber(ARGV[4])
local expiry = burst_window
if count >= limit then
  expiry = math.min(lockout_max, lockout_base * (2 ^ (count - limit)))
end
redis.call('EXPIRE', KEYS[1], expiry)
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


def _lockout_seconds(attempts: int) -> int:
    count = max(0, int(attempts))
    if count < LOGIN_FAILURE_LIMIT:
        return LOGIN_BURST_WINDOW_SECONDS
    exponent = min(16, count - LOGIN_FAILURE_LIMIT)
    return min(LOGIN_LOCKOUT_MAX_SECONDS, LOGIN_LOCKOUT_BASE_SECONDS * (2**exponent))


def _ensure_expiry(client: Redis, redis_key: str, attempts: int, ttl: int) -> int:
    if ttl > 0:
        return ttl
    expiry = _lockout_seconds(attempts)
    client.expire(redis_key, expiry)
    return expiry


def check_login_rate_limit(redis_url: str, key: str) -> RateLimitDecision:
    redis_key = _redis_key(key)
    try:
        client = _client(redis_url)
        raw_count = client.get(redis_key)
        count = int(raw_count or 0)
        ttl = int(client.ttl(redis_key)) if count else 0
        if count and ttl <= 0:
            ttl = _ensure_expiry(client, redis_key, count, ttl)
        if count < LOGIN_FAILURE_LIMIT:
            return RateLimitDecision(allowed=True, attempts=count)
        return RateLimitDecision(
            allowed=False,
            retry_after=ttl,
            attempts=count,
        )
    except (RedisError, OSError, ValueError) as exc:
        raise RateLimitUnavailable("login rate limiter unavailable") from exc


def record_login_failure(redis_url: str, key: str) -> RateLimitDecision:
    redis_key = _redis_key(key)
    try:
        client = _client(redis_url)
        count, ttl = client.eval(
            _RECORD_FAILURE_SCRIPT,
            1,
            redis_key,
            LOGIN_FAILURE_LIMIT,
            LOGIN_BURST_WINDOW_SECONDS,
            LOGIN_LOCKOUT_BASE_SECONDS,
            LOGIN_LOCKOUT_MAX_SECONDS,
        )
        count_i = int(count)
        ttl_i = _ensure_expiry(client, redis_key, count_i, int(ttl))
        return RateLimitDecision(
            allowed=count_i < LOGIN_FAILURE_LIMIT,
            retry_after=0 if count_i < LOGIN_FAILURE_LIMIT else ttl_i,
            attempts=count_i,
        )
    except (RedisError, OSError, ValueError, TypeError) as exc:
        raise RateLimitUnavailable("login rate limiter unavailable") from exc


def clear_login_rate_limit(redis_url: str, key: str) -> None:
    try:
        _client(redis_url).delete(_redis_key(key))
    except (RedisError, OSError, ValueError) as exc:
        raise RateLimitUnavailable("login rate limiter unavailable") from exc
