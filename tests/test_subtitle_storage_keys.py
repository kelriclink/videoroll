from __future__ import annotations

from videoroll.apps.subtitle_service.worker import _unique_storage_key


def test_worker_storage_keys_are_unique_per_upload_operation() -> None:
    first = _unique_storage_key("final/task/video_burnin", "a" * 64, ".mp4")
    second = _unique_storage_key("final/task/video_burnin", "a" * 64, ".mp4")

    assert first.startswith("final/task/video_burnin_aaaaaaaaaaaaaaaa_")
    assert first.endswith(".mp4")
    assert first != second
