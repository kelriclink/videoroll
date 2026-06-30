from __future__ import annotations

from unittest.mock import patch

import pytest

from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliRateLimitError, _bili_code_ok
from videoroll.apps.bilibili_publisher import worker


class _FakeLock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.released = False

    def acquire(self, blocking: bool = True) -> bool:
        return self.acquired

    def release(self) -> None:
        self.released = True
        return None


class _FakeRedis:
    def __init__(self, *, lock_acquired: bool = True) -> None:
        self.lock_acquired = lock_acquired
        self.store: dict[str, str] = {}

    def lock(self, *args, **kwargs) -> _FakeLock:
        return _FakeLock(acquired=self.lock_acquired)

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


def test_bili_code_ok_raises_rate_limit_for_submit_702() -> None:
    with pytest.raises(BilibiliRateLimitError) as exc:
        _bili_code_ok(
            {"code": -702, "message": "请求频率过高，请稍后再试"},
            status_code=200,
            rate_limit_scope="submit",
        )

    assert exc.value.code == -702
    assert exc.value.scope == "submit"


def test_compute_publish_throttle_interval_scales_with_pending_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BILIBILI_PUBLISH_UPLOAD_BASE_SECONDS",
        "BILIBILI_PUBLISH_UPLOAD_QUEUE_STEP_SECONDS",
        "BILIBILI_PUBLISH_UPLOAD_MAX_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert worker._compute_publish_throttle_interval("upload", pending_jobs=1) == 45.0
    assert worker._compute_publish_throttle_interval("upload", pending_jobs=5) == 105.0
    assert worker._compute_publish_throttle_interval("upload", pending_jobs=20) == 180.0


def test_rate_limit_stage_detects_upload_from_601() -> None:
    err = BilibiliRateLimitError(code=601, message="您上传视频过快，请您稍作休息后再继续", status_code=406)
    assert worker._rate_limit_stage(err) == "upload"


def test_reserve_publish_stage_slot_uses_queue_depth_to_push_later_calls() -> None:
    fake_redis = _FakeRedis()
    with (
        patch.object(worker, "_redis_client", return_value=fake_redis),
        patch.object(worker, "_count_pending_publish_jobs", return_value=3),
        patch.object(worker.time, "time", return_value=1000.0),
    ):
        first = worker._reserve_publish_stage_slot(None, stage="upload", job_id="job-1")  # type: ignore[arg-type]
        second = worker._reserve_publish_stage_slot(None, stage="upload", job_id="job-2")  # type: ignore[arg-type]

    assert first["wait_seconds"] == 0.0
    assert first["interval_seconds"] == 75.0
    assert second["wait_seconds"] == 75.0
    assert second["source"] == "redis"


def test_try_acquire_publish_job_lock_returns_false_when_already_running() -> None:
    fake_redis = _FakeRedis(lock_acquired=False)
    with patch.object(worker, "_redis_client", return_value=fake_redis):
        assert worker._try_acquire_publish_job_lock("job-1") is False


def test_try_acquire_publish_job_lock_returns_lock_when_acquired() -> None:
    fake_redis = _FakeRedis(lock_acquired=True)
    with patch.object(worker, "_redis_client", return_value=fake_redis):
        lock = worker._try_acquire_publish_job_lock("job-1")

    assert isinstance(lock, _FakeLock)
