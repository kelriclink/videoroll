from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from videoroll.apps.subtitle_service.worker import cleanup_task
from videoroll.db.models import PublishBatch, RenderJob, SubtitleJob, Task


def test_cleanup_retries_and_clears_marker_when_subtitle_work_is_in_flight() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    task = MagicMock(id=task_id, active_publish_batch_id=batch_id)
    batch = MagicMock(id=batch_id, task_id=task_id, state="succeeded", cleanup_enqueued_at=MagicMock())

    publish_query = MagicMock()
    publish_query.filter.return_value.count.return_value = 0
    subtitle_query = MagicMock()
    subtitle_query.filter.return_value.count.return_value = 1
    render_query = MagicMock()
    render_query.filter.return_value.count.return_value = 0

    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model is Task else batch if model is PublishBatch else None
    db.query.side_effect = [publish_query, subtitle_query, render_query]

    with (
        patch("videoroll.apps.subtitle_service.worker._ensure_db"),
        patch("videoroll.apps.subtitle_service.worker._db", return_value=db),
        patch("videoroll.apps.subtitle_service.worker.S3Store") as store_cls,
        patch.object(cleanup_task, "retry", side_effect=Retry()),
    ):
        store_cls.return_value.ensure_bucket.return_value = None
        with pytest.raises(Retry):
            cleanup_task.run(str(task_id), str(batch_id))

    assert batch.cleanup_enqueued_at is None
    db.commit.assert_called_once()
