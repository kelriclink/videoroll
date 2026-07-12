from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from videoroll.apps.orchestrator_api.schemas import PublishBatchSummary
from videoroll.apps.orchestrator_api.services.publishing_service import list_task_publish_batches
from videoroll.db.models import Platform, PublishState


def test_publish_batch_summary_exposes_state_and_expected_targets() -> None:
    batch_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    target = {"key": "douyin:default", "platform": "douyin", "account_id": None}

    summary = PublishBatchSummary.model_validate(
        {
            "id": batch_id,
            "task_id": uuid.uuid4(),
            "state": "partial_failed",
            "expected_targets": [target],
            "outcomes": {"douyin:default": {"state": "failed"}},
            "cleanup_enqueued_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )

    assert summary.id == batch_id
    assert summary.expected_targets == [target]
    assert summary.outcomes["douyin:default"]["state"] == "failed"


def test_publish_batch_list_includes_worker_failure_for_each_target() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    batch = MagicMock(
        id=batch_id,
        task_id=task_id,
        state="partial_failed",
        expected_targets=[{"key": "douyin:default", "platform": "douyin", "account_id": None}],
        outcomes_json={},
        cleanup_enqueued_at=None,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    job = MagicMock(
        id=uuid.uuid4(),
        batch_id=batch_id,
        platform=Platform.douyin,
        account_id=None,
        state=PublishState.failed,
        response_json={"error": "browser upload failed"},
        updated_at=now,
        created_at=now,
    )
    batch_query = MagicMock()
    batch_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [batch]
    job_query = MagicMock()
    job_query.filter.return_value.order_by.return_value.all.return_value = [job]
    db = MagicMock()
    db.get.return_value = MagicMock()
    db.query.side_effect = [batch_query, job_query]

    result = list_task_publish_batches(task_id, 20, db)

    assert result[0]["outcomes"]["douyin:default"]["state"] == "failed"
    assert result[0]["outcomes"]["douyin:default"]["detail"] == "browser upload failed"
