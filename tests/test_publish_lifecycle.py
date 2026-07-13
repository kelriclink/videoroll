from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from videoroll.apps.publish_lifecycle import (
    PublishBatchState,
    bind_unresolved_social_publish_target,
    enqueue_publish_batch_cleanup,
    evaluate_publish_batch,
    publish_target_key,
    reconcile_publish_batch,
)
from videoroll.apps.orchestrator_api.services.publishing_service import reconcile_published_task_state
from videoroll.apps.orchestrator_api.schemas import PublishAllRequest
from videoroll.db.models import Platform, PublishBatch, PublishJob, PublishState, Task, TaskStatus


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
    assert "cleanup_delivery_version" in PublishBatch.__table__.c
    assert "batch_id" in PublishJob.__table__.c
    assert "active_publish_batch_id" in Task.__table__.c


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


def test_late_non_current_batch_cannot_overwrite_task_publish_state() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    task = MagicMock(id=task_id, status=TaskStatus.publishing, active_publish_batch_id=uuid.uuid4())
    batch = MagicMock(
        id=batch_id,
        task_id=task_id,
        expected_targets=[_target("bilibili")],
        outcomes_json={},
        cleanup_enqueued_at=None,
    )
    job = MagicMock(platform=Platform.bilibili, account_id=None, state=PublishState.published)
    query = MagicMock()
    query.filter.return_value.order_by.return_value.all.return_value = [job]
    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: batch if model is PublishBatch else task
    db.query.return_value = query

    reconciliation = reconcile_publish_batch(db, batch_id)

    assert reconciliation.batch_state == PublishBatchState.succeeded
    assert reconciliation.cleanup_needed is False
    assert task.status == TaskStatus.publishing
    locked_models = [call.args[0] for call in db.get.call_args_list if call.kwargs.get("with_for_update")]
    assert locked_models == [Task, PublishBatch]


def test_batch_managed_task_skips_legacy_any_published_job_compensation() -> None:
    task = MagicMock(status=TaskStatus.publishing, active_publish_batch_id=uuid.uuid4())
    db = MagicMock()

    changed = reconcile_published_task_state(db, task)

    assert changed is False
    db.query.assert_not_called()


def test_cleanup_broker_failure_keeps_durable_dispatch_intent() -> None:
    db = MagicMock()
    celery_app = MagicMock()
    celery_app.send_task.side_effect = RuntimeError("broker unavailable")

    with patch("videoroll.apps.publish_lifecycle.mark_publish_batch_cleanup_enqueued") as mark:
        assert enqueue_publish_batch_cleanup(db, celery_app, uuid.uuid4(), uuid.uuid4(), needed=True) is True

    mark.assert_not_called()
    db.commit.assert_called_once()


def test_binding_unresolved_social_target_uses_current_batch_under_task_lock() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    account_id = uuid.uuid4()
    task = MagicMock(id=task_id, active_publish_batch_id=batch_id)
    batch = MagicMock(
        id=batch_id,
        task_id=task_id,
        expected_targets=[_target("douyin")],
        outcomes_json={"douyin:default": {"state": "failed"}},
    )
    no_job_query = MagicMock()
    no_job_query.filter.return_value.first.return_value = None
    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model is Task else batch
    db.query.return_value = no_job_query

    rebound = bind_unresolved_social_publish_target(
        db,
        batch_id,
        platform="douyin",
        account_id=account_id,
    )

    assert rebound["key"] == f"douyin:{account_id}"
    assert batch.expected_targets == [rebound]
    assert batch.outcomes_json == {}
    locked_models = [call.args[0] for call in db.get.call_args_list if call.kwargs.get("with_for_update")]
    assert locked_models == [Task, PublishBatch]


def test_publish_all_request_parses_false_string_as_false() -> None:
    payload = PublishAllRequest.model_validate(
        {"skip_review": "false", "force_retry": "false"}
    )

    assert payload.skip_review is False
    assert payload.force_retry is False
