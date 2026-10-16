from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from videoroll.apps.bilibili_publisher import worker
from videoroll.db.models import PublishJob, PublishState, SourceType, Task, TaskStatus


def test_worker_never_executes_unknown_publish_job() -> None:
    job = PublishJob(id=uuid.uuid4(), task_id=uuid.uuid4(), state=PublishState.unknown)
    db = MagicMock()
    db.get.return_value = job
    with (
        patch.object(worker, "_ensure_db"),
        patch.object(worker, "_db", return_value=db),
        patch.object(worker, "acquire_job_lease") as acquire,
    ):
        result = worker._process_job_impl(MagicMock(), str(job.id))
    assert result == {"status": "skipped", "state": "unknown"}
    acquire.assert_not_called()


def test_archive_response_timeout_requires_manual_confirmation(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    task = Task(id=uuid.uuid4(), source_type=SourceType.local, status=TaskStatus.publishing)
    job = PublishJob(
        id=uuid.uuid4(), task_id=task.id, state=PublishState.submitting,
        meta_json={"meta": {}, "video_key": "video.mp4", "typeid_mode": "meta"},
    )
    db = MagicMock()
    db.get.side_effect = lambda cls, _id: job if cls is PublishJob else task
    store = MagicMock()
    store.path_for.return_value = video
    uploaded = SimpleNamespace(filename_no_suffix="video", upload_id="upload", cid=7)
    client = MagicMock()
    client.upload_video_file.return_value = (uploaded, {})
    web_client = MagicMock()
    web_client.__enter__.return_value = client
    with (
        patch.object(worker, "_ensure_db"),
        patch.object(worker, "_db", return_value=db),
        patch.object(worker, "acquire_job_lease", return_value=True),
        patch.object(worker, "release_job_lease"),
        patch.object(worker, "JobLeaseHeartbeat"),
        patch.object(worker, "_latest_published_job", return_value=None),
        patch.object(worker, "get_bilibili_cookie_header", return_value="cookie"),
        patch.object(worker, "get_bilibili_csrf_token", return_value="csrf"),
        patch.object(worker.BilibiliPublishMeta, "model_validate", return_value=SimpleNamespace(title="test", typeid=1)),
        patch.object(worker, "FileStore", return_value=store),
        patch.object(worker, "BilibiliWebClient", return_value=web_client),
        patch.object(worker, "_set_bilibili_upload_progress"),
        patch.object(worker, "_apply_publish_stage_throttle", return_value={}),
        patch.object(worker, "_add_archive_with_desc_retry", side_effect=httpx.ReadTimeout("archive response lost")),
        patch.object(worker, "_can_mark_task_publish_failed", return_value=True),
        patch.object(worker, "_ensure_publish_result_asset"),
    ):
        result = worker._process_job_impl(MagicMock(), str(job.id))

    assert result["status"] == "unknown"
    assert job.state == PublishState.unknown
    assert job.response_json["requires_manual_confirmation"] is True
    assert task.error_code == "PUBLISH_OUTCOME_UNKNOWN"
