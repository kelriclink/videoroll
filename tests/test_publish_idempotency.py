from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from videoroll.apps.bilibili_publisher.main import publish as publish_bilibili
from videoroll.apps.bilibili_publisher.schemas import PublishRequest
from videoroll.apps.social_publisher.main import publish
from videoroll.apps.social_publisher.schemas import SocialPublishRequest
from videoroll.db.models import Platform, PublishBatch, PublishState, Task


def test_force_retry_does_not_duplicate_a_submitted_social_job() -> None:
    task_id = uuid.uuid4()
    account_id = uuid.uuid4()
    existing = MagicMock(
        id=uuid.uuid4(),
        platform=Platform.douyin,
        state=PublishState.submitted,
        external_id="douyin-post-1",
        external_url="https://example.invalid/douyin-post-1",
        response_json={"driver": "sau"},
    )
    task = MagicMock(id=task_id)
    account = MagicMock(id=account_id, platform=Platform.douyin, is_active=True, check_state="valid")
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value.first.return_value = existing
    db = MagicMock()
    db.get.side_effect = lambda _model, item, **_kwargs: task if item == task_id else account
    db.query.return_value = query

    result = publish(
        "douyin",
        SocialPublishRequest(
            platform="douyin",
            task_id=task_id,
            account_id=account_id,
            video={"key": "final.mp4"},
            meta={"title": "title", "tags": ["tag"]},
            force_retry=True,
        ),
        settings=MagicMock(),
        db=db,
    )

    assert result.job_id == existing.id
    assert result.state == PublishState.submitted.value
    db.add.assert_not_called()


def test_bilibili_publish_rejects_a_batch_owned_by_another_task() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    task = MagicMock(id=task_id, active_publish_batch_id=batch_id)
    batch = MagicMock(id=batch_id, task_id=uuid.uuid4())
    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model is Task else batch if model is PublishBatch else None
    db.query.side_effect = AssertionError("batch validation must run before publish job lookup")

    with pytest.raises(HTTPException) as exc_info:
        publish_bilibili(
            PublishRequest(
                task_id=task_id,
                batch_id=batch_id,
                video={"type": "s3", "key": "final.mp4"},
                meta={"title": "title", "typeid": 17, "tags": ["tag"]},
            ),
            settings=MagicMock(),
            db=db,
        )

    assert exc_info.value.status_code == 400
    db.query.assert_not_called()


def test_social_publish_rejects_an_inactive_batch() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    account_id = uuid.uuid4()
    task = MagicMock(id=task_id, active_publish_batch_id=uuid.uuid4())
    account = MagicMock(id=account_id, platform=Platform.douyin, is_active=True, check_state="valid")
    batch = MagicMock(id=batch_id, task_id=task_id)
    db = MagicMock()

    def get(model, item_id, **_kwargs):
        if model is Task:
            return task
        if model is PublishBatch:
            return batch
        return account

    db.get.side_effect = get
    db.query.side_effect = AssertionError("batch validation must run before publish job lookup")

    with pytest.raises(HTTPException) as exc_info:
        publish(
            "douyin",
            SocialPublishRequest(
                platform="douyin",
                task_id=task_id,
                batch_id=batch_id,
                account_id=account_id,
                video={"key": "final.mp4"},
                meta={"title": "title", "tags": ["tag"]},
            ),
            settings=MagicMock(),
            db=db,
        )

    assert exc_info.value.status_code == 409
    db.query.assert_not_called()


def test_bilibili_publish_rejects_a_target_outside_the_current_batch() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    task = MagicMock(id=task_id, active_publish_batch_id=batch_id)
    batch = MagicMock(
        id=batch_id,
        task_id=task_id,
        expected_targets=[{"key": "douyin:default", "platform": "douyin", "account_id": None}],
    )
    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model is Task else batch if model is PublishBatch else None
    db.query.side_effect = AssertionError("target validation must run before publish job lookup")

    with pytest.raises(HTTPException) as exc_info:
        publish_bilibili(
            PublishRequest(
                task_id=task_id,
                batch_id=batch_id,
                video={"type": "s3", "key": "final.mp4"},
                meta={"title": "title", "typeid": 17, "tags": ["tag"]},
            ),
            settings=MagicMock(),
            db=db,
        )

    assert exc_info.value.status_code == 400
    db.query.assert_not_called()


def test_social_publish_rejects_an_account_outside_the_current_batch() -> None:
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    expected_account_id = uuid.uuid4()
    requested_account_id = uuid.uuid4()
    task = MagicMock(id=task_id, active_publish_batch_id=batch_id)
    batch = MagicMock(
        id=batch_id,
        task_id=task_id,
        expected_targets=[
            {
                "key": f"douyin:{expected_account_id}",
                "platform": "douyin",
                "account_id": str(expected_account_id),
            }
        ],
    )
    account = MagicMock(id=requested_account_id, platform=Platform.douyin, is_active=True, check_state="valid")
    db = MagicMock()

    def get(model, _item_id, **_kwargs):
        if model is Task:
            return task
        if model is PublishBatch:
            return batch
        return account

    db.get.side_effect = get
    db.query.side_effect = AssertionError("target validation must run before publish job lookup")

    with pytest.raises(HTTPException) as exc_info:
        publish(
            "douyin",
            SocialPublishRequest(
                platform="douyin",
                task_id=task_id,
                batch_id=batch_id,
                account_id=requested_account_id,
                video={"key": "final.mp4"},
                meta={"title": "title", "tags": ["tag"]},
            ),
            settings=MagicMock(),
            db=db,
        )

    assert exc_info.value.status_code == 400
    db.query.assert_not_called()
