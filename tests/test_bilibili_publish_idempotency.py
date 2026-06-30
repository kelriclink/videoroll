from __future__ import annotations

import uuid
from unittest.mock import patch

from videoroll.apps.bilibili_publisher import worker
from videoroll.db.models import PublishJob, PublishState, SourceLicense, SourceType, Task, TaskStatus


def _task(*, status: TaskStatus = TaskStatus.publishing) -> Task:
    return Task(
        id=uuid.uuid4(),
        source_type=SourceType.youtube,
        source_license=SourceLicense.own,
        status=status,
        error_code="PUBLISH_FAILED",
        error_message="old failure",
    )


def test_mirror_published_job_marks_duplicate_as_published_without_resubmitting() -> None:
    task = _task(status=TaskStatus.failed)
    published = PublishJob(
        id=uuid.uuid4(),
        task_id=task.id,
        state=PublishState.published,
        aid="123",
        bvid="BV123",
        response_json={"mode": "web", "result": {"aid": "123", "bvid": "BV123"}},
    )
    duplicate = PublishJob(id=uuid.uuid4(), task_id=task.id, state=PublishState.submitting)

    worker._mirror_published_job(duplicate, published, task)

    assert duplicate.state == PublishState.published
    assert duplicate.aid == "123"
    assert duplicate.bvid == "BV123"
    assert duplicate.response_json["skipped_duplicate_publish"] is True
    assert task.status == TaskStatus.published
    assert task.error_code is None
    assert task.error_message is None


def test_published_task_cannot_be_marked_publish_failed() -> None:
    task = _task(status=TaskStatus.published)

    assert worker._can_mark_task_publish_failed(None, task) is False  # type: ignore[arg-type]


def test_task_with_published_job_cannot_be_marked_publish_failed() -> None:
    task = _task(status=TaskStatus.publishing)

    with patch.object(worker, "_task_has_published_job", return_value=True):
        assert worker._can_mark_task_publish_failed(None, task) is False  # type: ignore[arg-type]
