"""Tests for videoroll.apps.publish_service."""

import uuid
from unittest.mock import MagicMock, patch
from pathlib import Path

import httpx

from videoroll.apps.orchestrator_api.schemas import PublishAllRequest
from videoroll.apps.orchestrator_api.services.publishing_service import publish_all
from videoroll.apps.publish_service import PublishAllResult, PublishService
from videoroll.apps.publish_lifecycle import PublishBatchState
from videoroll.db.models import Account, Platform, TaskStatus


# ── PublishAllResult ───────────────────────────────────────────


def test_empty_result():
    r = PublishAllResult()
    assert r.all_ok is False
    assert r.has_any_ok is False
    assert r.platform_count == 0
    assert r.ok_count == 0
    assert r.error_count == 0
    assert r.errors == {}


def test_all_ok():
    r = PublishAllResult(
        results={
            "bilibili": {"status": "ok", "bvid": "BV_test1"},
            "douyin": {"status": "ok", "external_id": "dy_123"},
        }
    )
    assert r.all_ok is True
    assert r.has_any_ok is True
    assert r.platform_count == 2
    assert r.ok_count == 2
    assert r.error_count == 0
    assert r.errors == {}


def test_partial_failure():
    r = PublishAllResult(
        results={
            "bilibili": {"status": "ok"},
            "douyin": {"status": "error", "detail": "no account configured"},
        }
    )
    assert r.all_ok is False
    assert r.has_any_ok is True
    assert r.platform_count == 2
    assert r.ok_count == 1
    assert r.error_count == 1
    assert r.errors == {"douyin": "no account configured"}


def test_all_failed():
    r = PublishAllResult(
        results={
            "bilibili": {"status": "error", "detail": "timeout"},
            "douyin": {"status": "error", "detail": "no account"},
        }
    )
    assert r.all_ok is False
    assert r.has_any_ok is False
    assert r.error_count == 2
    assert set(r.errors.keys()) == {"bilibili", "douyin"}


def test_skipped_counts_as_non_ok():
    r = PublishAllResult(
        results={
            "bilibili": {"status": "ok"},
            "douyin": {"status": "skipped", "detail": "already published"},
        }
    )
    assert r.all_ok is False
    assert r.has_any_ok is True
    assert r.errors == {"douyin": "already published"}


def test_accepted_results_are_not_reported_as_published() -> None:
    r = PublishAllResult(
        results={
            "bilibili": {"status": "accepted", "state": "published"},
            "douyin": {"status": "accepted", "state": "submitting"},
        }
    )

    assert r.all_accepted is True
    assert r.has_any_accepted is True
    assert r.all_published is False


def test_publish_service_does_not_import_orchestrator_api() -> None:
    source = Path("src/videoroll/apps/publish_service.py").read_text(encoding="utf-8")

    assert "orchestrator_api" not in source
    assert "subtitle_service.worker" not in source


# ── PublishService.publish() ──────────────────────────────────


def _publish_batch(*targets: tuple[str, str | None]) -> tuple[MagicMock, list[dict[str, str | None]]]:
    batch = MagicMock()
    batch.id = uuid.uuid4()
    batch.request_json = {}
    return batch, [
        {"platform": platform, "account_id": account_id, "key": f"{platform}:{account_id or 'default'}"}
        for platform, account_id in targets
    ]


@patch("videoroll.apps.publish_service.get_publish_platform_settings")
@patch.object(PublishService, "_build_backend_request")
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_calls_all_enabled_platforms(mock_client_cls, mock_build, mock_get_settings):
    mock_get_settings.return_value = {
        "bilibili": True,
        "douyin": True,
        "xiaohongshu": False,
        "kuaishou": False,
    }
    mock_build.return_value = {
        "task_id": "test",
        "video": {"type": "s3", "key": "k"},
        "meta": {"title": "t", "typeid": 17},
    }

    mock_resp = MagicMock()
    mock_resp.content = b'{"bvid": "test"}'
    mock_resp.json.return_value = {"bvid": "test", "platform": "bilibili"}
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    db = MagicMock()
    task = MagicMock()
    task.status = TaskStatus.rendered
    db.get.return_value = task

    svc = PublishService(db, MagicMock(), MagicMock())
    bilibili_batch, _ = _publish_batch(("bilibili", None))
    douyin_batch, _ = _publish_batch(("douyin", None))
    reconciliation = MagicMock(cleanup_needed=False)
    with (
        patch.object(svc, "_get_or_create_single_target_batch", side_effect=[bilibili_batch, douyin_batch]) as get_batch,
        patch.object(svc, "_latest_batch_target_job", return_value=None),
        patch.object(svc, "_resolve_social_account_id", side_effect=ValueError("no account")),
        patch("videoroll.apps.publish_service.reconcile_publish_batch", return_value=reconciliation),
    ):
        result = svc.publish(uuid.uuid4())

    assert result.platform_count == 2
    assert "bilibili" in result.results
    assert "douyin" in result.results
    assert mock_client.post.call_count == 2
    assert mock_build.call_count == 2
    assert get_batch.call_count == 2
    assert get_batch.call_args_list[0].args[1] == "bilibili"
    assert get_batch.call_args_list[1].args[1] == "douyin"


def test_single_platform_batch_is_not_blocked_by_a_different_platform_batch() -> None:
    task_id = uuid.uuid4()
    task = MagicMock(id=task_id)
    new_batch = MagicMock(id=uuid.uuid4())
    db = MagicMock()
    db.get.return_value = task
    service = PublishService(db, MagicMock(), MagicMock())

    with (
        patch("videoroll.apps.publish_service.latest_publish_batch_for_target", return_value=None) as latest,
        patch.object(service, "_create_batch", return_value=new_batch) as create_batch,
    ):
        result = service._get_or_create_single_target_batch(task_id, "bilibili", None, {})

    assert result is new_batch
    latest.assert_called_once_with(db, task_id, platform="bilibili", account_id=None)
    assert create_batch.call_args.args[1] == [
        {"key": "bilibili:default", "platform": "bilibili", "account_id": None}
    ]


@patch("videoroll.apps.publish_service.get_publish_platform_settings")
def test_publish_no_enabled_platforms(mock_get_settings):
    mock_get_settings.return_value = {
        "bilibili": False,
        "douyin": False,
        "xiaohongshu": False,
        "kuaishou": False,
    }
    db = MagicMock()
    svc = PublishService(db, MagicMock(), MagicMock())
    result = svc.publish(uuid.uuid4())
    assert result.platform_count == 0
    assert result.has_any_ok is False


def test_publish_all_social_only_does_not_require_bilibili_meta() -> None:
    task_id = uuid.uuid4()
    task = MagicMock(id=task_id)
    task.source_license.value = "own"
    db = MagicMock()
    db.get.return_value = task
    result = PublishAllResult(
        batch_id=str(uuid.uuid4()),
        results={"douyin": {"status": "accepted", "state": "submitting"}},
    )

    with (
        patch(
            "videoroll.apps.orchestrator_api.services.publishing_service.get_publish_platform_settings",
            return_value={"bilibili": False, "douyin": True},
        ),
        patch(
            "videoroll.apps.orchestrator_api.services.publishing_service.prepare_publish_meta",
            side_effect=AssertionError("social-only publishing must not validate Bilibili meta"),
        ),
        patch.object(PublishService, "publish", return_value=result),
    ):
        response = publish_all(
            task_id,
            PublishAllRequest(
                platform_meta={"douyin": {"title": "title", "tags": ["tag"]}},
                skip_review=True,
            ),
            MagicMock(),
            db,
            MagicMock(),
        )

    assert response["results"]["douyin"]["status"] == "accepted"


def test_publish_all_social_only_reviews_stored_platform_meta() -> None:
    task_id = uuid.uuid4()
    task = MagicMock(id=task_id)
    task.source_license.value = "own"
    db = MagicMock()
    db.get.return_value = task
    stored_meta = {"title": "stored title", "tags": ["tag"]}
    result = PublishAllResult(
        batch_id=str(uuid.uuid4()),
        results={"douyin": {"status": "accepted", "state": "submitting"}},
    )

    with (
        patch(
            "videoroll.apps.orchestrator_api.services.publishing_service.get_publish_platform_settings",
            return_value={"bilibili": False, "douyin": True},
        ),
        patch(
            "videoroll.apps.orchestrator_api.services.publishing_service.read_s3_json_object",
            return_value=stored_meta,
        ),
        patch(
            "videoroll.apps.orchestrator_api.services.publishing_service.run_task_publish_review",
            return_value={"ok": True},
        ) as review,
        patch.object(PublishService, "publish", return_value=result),
    ):
        publish_all(
            task_id,
            PublishAllRequest(),
            MagicMock(),
            db,
            MagicMock(),
        )

    assert review.call_args.kwargs["meta"] == stored_meta


def test_force_retry_reuses_the_current_publish_batch() -> None:
    """A retry must not race an in-flight or partially failed batch."""
    task_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    task = MagicMock(id=task_id, active_publish_batch_id=batch_id)
    batch = MagicMock(id=batch_id, task_id=task_id, state=PublishBatchState.failed.value)
    batch.expected_targets = [{"key": "bilibili:default", "platform": "bilibili", "account_id": None}]

    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model.__name__ == "Task" else batch
    service = PublishService(db, MagicMock(), MagicMock())

    with patch.object(service, "_get_enabled_platforms", return_value=["bilibili"]):
        actual_batch, targets = service._get_or_create_batch(task_id, {"force_retry": True})

    assert actual_batch is batch
    assert targets == batch.expected_targets
    db.add.assert_not_called()
    db.query.assert_not_called()


def test_new_batch_assigns_a_real_id_to_the_task_before_commit() -> None:
    task_id = uuid.uuid4()
    task = MagicMock(active_publish_batch_id=None)
    db = MagicMock()
    db.get.return_value = task
    service = PublishService(db, MagicMock(), MagicMock())

    batch = service._create_batch(
        task_id,
        [{"key": "bilibili:default", "platform": "bilibili", "account_id": None}],
        {},
    )

    assert isinstance(batch.id, uuid.UUID)
    assert task.active_publish_batch_id == batch.id


def test_publish_one_rejects_a_batch_owned_by_another_task() -> None:
    task_id = uuid.uuid4()
    batch = MagicMock(id=uuid.uuid4(), task_id=uuid.uuid4())
    db = MagicMock()
    db.get.return_value = batch
    service = PublishService(db, MagicMock(), MagicMock())

    try:
        service.publish_one(task_id, platform="bilibili", payload={"batch_id": str(batch.id)})
    except ValueError as exc:
        assert str(exc) == "publish batch does not belong to this task"
    else:
        raise AssertionError("cross-task batch id must be rejected")

    db.commit.assert_not_called()


def test_completed_current_batch_does_not_revive_an_older_failed_batch() -> None:
    task_id = uuid.uuid4()
    completed = MagicMock(id=uuid.uuid4(), task_id=task_id, state=PublishBatchState.succeeded.value)
    task = MagicMock(id=task_id, active_publish_batch_id=completed.id)
    replacement = MagicMock(id=uuid.uuid4())
    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model.__name__ == "Task" else completed
    service = PublishService(db, MagicMock(), MagicMock())

    def create_replacement(*_args, **_kwargs):
        assert db.commit.call_count == 0
        return replacement

    with (
        patch.object(service, "_get_enabled_platforms", return_value=["bilibili"]),
        patch.object(service, "_create_batch", side_effect=create_replacement) as create_batch,
    ):
        batch, _targets = service._get_or_create_batch(task_id, {})

    assert batch is replacement
    create_batch.assert_called_once()
    db.query.assert_not_called()


def test_failed_batch_with_changed_targets_creates_a_replacement() -> None:
    task_id = uuid.uuid4()
    failed = MagicMock(id=uuid.uuid4(), task_id=task_id, state=PublishBatchState.failed.value)
    failed.expected_targets = [{"key": "bilibili:default", "platform": "bilibili", "account_id": None}]
    task = MagicMock(id=task_id, active_publish_batch_id=failed.id)
    replacement = MagicMock(id=uuid.uuid4())
    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model.__name__ == "Task" else failed
    service = PublishService(db, MagicMock(), MagicMock())

    with (
        patch.object(service, "_get_enabled_platforms", return_value=["douyin"]),
        patch.object(service, "_resolve_social_account_id", return_value=str(uuid.uuid4())),
        patch.object(service, "_create_batch", return_value=replacement) as create_batch,
    ):
        batch, targets = service._get_or_create_batch(task_id, {})

    assert batch is replacement
    assert targets[0]["platform"] == "douyin"
    create_batch.assert_called_once()


def test_failed_single_target_batch_allows_a_replacement_account() -> None:
    task_id = uuid.uuid4()
    old_account_id = uuid.uuid4()
    new_account_id = uuid.uuid4()
    failed = MagicMock(id=uuid.uuid4(), task_id=task_id, state=PublishBatchState.failed.value)
    failed.expected_targets = [
        {"key": f"douyin:{old_account_id}", "platform": "douyin", "account_id": str(old_account_id)}
    ]
    task = MagicMock(id=task_id, active_publish_batch_id=failed.id)
    replacement = MagicMock(id=uuid.uuid4())
    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_kwargs: task if model.__name__ == "Task" else failed
    service = PublishService(db, MagicMock(), MagicMock())

    with patch.object(service, "_create_batch", return_value=replacement) as create_batch:
        batch = service._get_or_create_single_target_batch(
            task_id,
            "douyin",
            str(new_account_id),
            {"account_id": str(new_account_id)},
        )

    assert batch is replacement
    create_batch.assert_called_once()


def test_retry_binds_an_unresolved_social_target_to_the_newly_valid_account() -> None:
    task_id = uuid.uuid4()
    account_id = uuid.uuid4()
    batch = MagicMock(id=uuid.uuid4())
    target = {"key": "douyin:default", "platform": "douyin", "account_id": None}
    batch.expected_targets = [target]
    batch.outcomes_json = {"douyin:default": {"state": "failed", "detail": "no valid account"}}
    db = MagicMock()
    service = PublishService(db, MagicMock(), MagicMock())

    expected = {
        "key": f"douyin:{account_id}",
        "platform": "douyin",
        "account_id": str(account_id),
    }
    with patch("videoroll.apps.publish_service.bind_unresolved_social_publish_target", return_value=expected) as bind:
        rebound = service._bind_unresolved_social_target(batch, target, str(account_id))

    assert rebound == expected
    bind.assert_called_once_with(db, batch.id, platform="douyin", account_id=str(account_id))
    db.commit.assert_called_once()


@patch("videoroll.apps.publish_service.get_publish_platform_settings")
@patch.object(PublishService, "_build_backend_request")
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_isolates_errors(mock_client_cls, mock_build, mock_get_settings):
    """One platform failing should not block others."""
    mock_get_settings.return_value = {
        "bilibili": True,
        "douyin": True,
        "xiaohongshu": False,
        "kuaishou": False,
    }
    mock_build.return_value = {
        "task_id": "test",
        "video": {"type": "s3", "key": "k"},
        "meta": {"title": "t", "typeid": 17},
    }

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            resp = MagicMock()
            resp.content = b'{"bvid": "ok"}'
            resp.json.return_value = {"bvid": "ok"}
            resp.raise_for_status = MagicMock()
            return resp
        raise RuntimeError("connection refused")

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = side_effect
    mock_client_cls.return_value = mock_client

    db = MagicMock()
    task = MagicMock()
    task.status = TaskStatus.rendered
    db.get.return_value = task

    svc = PublishService(db, MagicMock(), MagicMock())
    bilibili_batch, _ = _publish_batch(("bilibili", None))
    douyin_batch, _ = _publish_batch(("douyin", None))
    reconciliation = MagicMock(cleanup_needed=False)
    with (
        patch.object(svc, "_get_or_create_single_target_batch", side_effect=[bilibili_batch, douyin_batch]),
        patch.object(svc, "_latest_batch_target_job", return_value=None),
        patch.object(svc, "_resolve_social_account_id", side_effect=ValueError("no account")),
        patch("videoroll.apps.publish_service.reconcile_publish_batch", return_value=reconciliation),
        patch("videoroll.apps.publish_service.record_publish_batch_dispatch_error", return_value=reconciliation),
    ):
        result = svc.publish(uuid.uuid4())

    assert result.has_any_ok is True
    assert result.all_ok is False
    assert result.errors == {"douyin": "connection refused"}


@patch("videoroll.apps.publish_service.get_publish_platform_settings")
@patch.object(PublishService, "_build_backend_request")
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_rechecks_platform_when_task_is_already_published(
    mock_client_cls, mock_build, mock_get_settings
):
    mock_build.return_value = {
        "task_id": "test",
        "video": {"type": "s3", "key": "k"},
        "meta": {"title": "t", "typeid": 17},
    }
    mock_get_settings.return_value = {
        "bilibili": True,
        "douyin": False,
        "xiaohongshu": False,
        "kuaishou": False,
    }
    db = MagicMock()
    task = MagicMock()
    task.status = TaskStatus.published
    db.get.return_value = task

    response = MagicMock()
    response.content = b'{"state": "published", "bvid": "BV_existing"}'
    response.json.return_value = {"state": "published", "bvid": "BV_existing"}
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = response
    mock_client_cls.return_value = client

    svc = PublishService(db, MagicMock(), MagicMock())
    batch, _ = _publish_batch(("bilibili", None))
    reconciliation = MagicMock(cleanup_needed=False)
    with (
        patch.object(svc, "_get_or_create_single_target_batch", return_value=batch),
        patch.object(svc, "_latest_batch_target_job", return_value=None),
        patch("videoroll.apps.publish_service.reconcile_publish_batch", return_value=reconciliation),
    ):
        result = svc.publish(uuid.uuid4())

    assert result.results["bilibili"]["status"] == "accepted"
    client.post.assert_called_once()


@patch("videoroll.apps.publish_service.build_publish_gateway_request")
def test_build_backend_request_converts_auto_payload_to_gateway_request(mock_build):
    task_id = uuid.uuid4()
    task = MagicMock()
    db = MagicMock()
    db.get.return_value = task
    mock_build.return_value = {
        "task_id": str(task_id),
        "video": {"type": "s3", "key": "final.mp4"},
        "meta": {"title": "title", "typeid": 17},
    }

    service = PublishService(db, MagicMock(), MagicMock())
    result = service._build_backend_request(
        task_id,
        "bilibili",
        {
            "video_key": "final.mp4",
            "cover_key": "cover.jpg",
            "typeid_mode": "ai_summary",
            "meta": {"title": "title", "typeid": 17},
        },
    )

    request_payload = mock_build.call_args.kwargs["payload"]
    assert request_payload["platform"] == "bilibili"
    assert request_payload["video_key"] == "final.mp4"
    assert request_payload["cover_key"] == "cover.jpg"
    assert request_payload["typeid_mode"] == "ai_summary"
    assert result["video"] == {"type": "s3", "key": "final.mp4"}


@patch("videoroll.apps.publish_service.build_publish_gateway_request")
def test_build_backend_request_selects_valid_social_account(mock_build):
    task_id = uuid.uuid4()
    account_id = uuid.uuid4()
    task = MagicMock()
    account = MagicMock()
    account.id = account_id
    account.platform = Platform.douyin
    account.is_active = True
    account.check_state = "valid"

    account_query = MagicMock()
    account_query.filter.return_value.order_by.return_value.first.return_value = account
    db = MagicMock()
    db.get.return_value = task
    db.query.return_value = account_query
    mock_build.return_value = {"account_id": str(account_id)}

    service = PublishService(db, MagicMock(), MagicMock())
    service._build_backend_request(
        task_id,
        "douyin",
        {"video_key": "final.mp4", "meta": {"title": "title"}},
    )

    request_payload = mock_build.call_args.kwargs["payload"]
    assert request_payload["platform"] == "douyin"
    assert request_payload["account_id"] == str(account_id)
    db.query.assert_called_once_with(Account)


def test_http_status_error_detail_reports_validation_fields():
    request = httpx.Request("POST", "http://publisher/publish")
    response = httpx.Response(
        422,
        request=request,
        json={
            "detail": [
                {"loc": ["body", "video"], "msg": "Field required"},
                {"loc": ["body", "meta"], "msg": "Field required"},
            ]
        },
    )
    error = httpx.HTTPStatusError("unprocessable", request=request, response=response)

    detail = PublishService._http_status_error_detail(error)

    assert detail == "HTTP 422: video: Field required; meta: Field required"
