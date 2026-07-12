from __future__ import annotations

import uuid

from videoroll.apps.publish_lifecycle import (
    PublishBatchState,
    evaluate_publish_batch,
    publish_target_key,
)
from videoroll.apps.orchestrator_api.schemas import PublishAllRequest
from videoroll.db.models import PublishBatch, PublishJob, PublishState, TaskStatus


def _target(platform: str, account_id: uuid.UUID | None = None) -> dict[str, str | None]:
    return {
        "key": publish_target_key(platform, account_id),
        "platform": platform,
        "account_id": str(account_id) if account_id else None,
    }


def test_publish_batch_model_persists_expected_targets_and_job_link() -> None:
    assert "expected_targets" in PublishBatch.__table__.c
    assert "outcomes_json" in PublishBatch.__table__.c
    assert "cleanup_enqueued_at" in PublishBatch.__table__.c
    assert "batch_id" in PublishJob.__table__.c


def test_all_submitted_or_published_targets_complete_a_batch() -> None:
    account_id = uuid.uuid4()
    targets = [_target("bilibili"), _target("douyin", account_id)]

    result = evaluate_publish_batch(
        targets,
        {
            publish_target_key("bilibili", None): PublishState.published,
            publish_target_key("douyin", account_id): PublishState.submitted,
        },
        {},
    )

    assert result.batch_state == PublishBatchState.succeeded
    assert result.task_status == TaskStatus.published
    assert result.cleanup_ready is True


def test_partial_terminal_failure_keeps_task_publishing_for_channel_retry() -> None:
    account_id = uuid.uuid4()
    targets = [_target("bilibili"), _target("douyin", account_id)]

    result = evaluate_publish_batch(
        targets,
        {
            publish_target_key("bilibili", None): PublishState.published,
            publish_target_key("douyin", account_id): PublishState.failed,
        },
        {},
    )

    assert result.batch_state == PublishBatchState.partial_failed
    assert result.task_status == TaskStatus.publishing
    assert result.cleanup_ready is False


def test_all_terminal_failures_mark_task_failed() -> None:
    targets = [_target("bilibili"), _target("douyin")]

    result = evaluate_publish_batch(
        targets,
        {
            publish_target_key("bilibili", None): PublishState.failed,
            publish_target_key("douyin", None): PublishState.unknown,
        },
        {},
    )

    assert result.batch_state == PublishBatchState.failed
    assert result.task_status == TaskStatus.failed
    assert result.cleanup_ready is False


def test_dispatch_error_is_a_terminal_failure_without_a_publish_job() -> None:
    target = _target("douyin")

    result = evaluate_publish_batch(
        [target],
        {},
        {str(target["key"]): {"state": "failed", "detail": "no valid account"}},
    )

    assert result.batch_state == PublishBatchState.failed
    assert result.task_status == TaskStatus.failed


def test_publish_all_request_parses_false_string_as_false() -> None:
    payload = PublishAllRequest.model_validate(
        {"skip_review": "false", "force_retry": "false"}
    )

    assert payload.skip_review is False
    assert payload.force_retry is False
