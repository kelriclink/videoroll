from __future__ import annotations

from unittest.mock import patch

from videoroll.apps.subtitle_service.provider_rate_limit import ProviderRateGate


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def eval(self, _script: str, _num_keys: int, key: str, target: str, ttl: str) -> int:
        current = float(self.values.get(key, "0"))
        candidate = float(target)
        if candidate > current:
            self.values[key] = target
            self.ttls[key] = int(ttl)
            return 1
        return 0


def test_provider_cooldown_never_shortens_a_longer_shared_retry_after() -> None:
    redis = _FakeRedis()
    with patch("videoroll.apps.subtitle_service.provider_rate_limit.Redis.from_url", return_value=redis):
        gate = ProviderRateGate("redis://test", "groq", max_concurrency=1)

    with patch("videoroll.apps.subtitle_service.provider_rate_limit.time.time", side_effect=[1000.0, 1001.0]):
        gate.set_cooldown(60)
        gate.set_cooldown(10)

    assert float(redis.values[gate._cooldown_key]) == 1060.0
    assert redis.ttls[gate._cooldown_key] == 65


def test_provider_cooldown_extends_when_later_retry_after_is_longer() -> None:
    redis = _FakeRedis()
    with patch("videoroll.apps.subtitle_service.provider_rate_limit.Redis.from_url", return_value=redis):
        gate = ProviderRateGate("redis://test", "cloudflare", max_concurrency=2)

    with patch("videoroll.apps.subtitle_service.provider_rate_limit.time.time", side_effect=[2000.0, 2001.0]):
        gate.set_cooldown(10)
        gate.set_cooldown(60)

    assert float(redis.values[gate._cooldown_key]) == 2061.0
    assert redis.ttls[gate._cooldown_key] == 65
