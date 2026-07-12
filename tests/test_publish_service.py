"""Tests for videoroll.apps.publish_service."""

import uuid
from unittest.mock import MagicMock, patch

import httpx

from videoroll.apps.publish_service import PublishAllResult, PublishService
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


# ── PublishService.publish() ──────────────────────────────────


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
    result = svc.publish(uuid.uuid4())

    assert result.platform_count == 2
    assert "bilibili" in result.results
    assert "douyin" in result.results
    assert mock_client.post.call_count == 2
    assert mock_build.call_count == 2


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
    result = svc.publish(uuid.uuid4())

    assert result.results["bilibili"]["status"] == "ok"
    client.post.assert_called_once()


@patch(
    "videoroll.apps.orchestrator_api.services.publishing_service."
    "build_publish_gateway_request"
)
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
    assert request_payload.platform == "bilibili"
    assert request_payload.video_key == "final.mp4"
    assert request_payload.cover_key == "cover.jpg"
    assert request_payload.typeid_mode == "ai_summary"
    assert result["video"] == {"type": "s3", "key": "final.mp4"}


@patch(
    "videoroll.apps.orchestrator_api.services.publishing_service."
    "build_publish_gateway_request"
)
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
    assert request_payload.platform == "douyin"
    assert request_payload.account_id == str(account_id)
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
