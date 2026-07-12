"""Tests for videoroll.apps.publish_service."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from videoroll.apps.publish_service import PublishAllResult, PublishService
from videoroll.db.models import TaskStatus


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
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_calls_all_enabled_platforms(mock_client_cls, mock_get_settings):
    mock_get_settings.return_value = {
        "bilibili": True,
        "douyin": True,
        "xiaohongshu": False,
        "kuaishou": False,
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
    settings = MagicMock()
    s3 = MagicMock()

    svc = PublishService(db, settings, s3)
    result = svc.publish(uuid.uuid4())

    assert result.platform_count == 2
    assert "bilibili" in result.results
    assert "douyin" in result.results
    assert mock_client.post.call_count == 2


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
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_isolates_errors(mock_client_cls, mock_get_settings):
    """One platform failing should not block others."""
    mock_get_settings.return_value = {
        "bilibili": True,
        "douyin": True,
        "xiaohongshu": False,
        "kuaishou": False,
    }

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # bilibili succeeds
            resp = MagicMock()
            resp.content = b'{"bvid": "ok"}'
            resp.json.return_value = {"bvid": "ok"}
            resp.raise_for_status = MagicMock()
            return resp
        # douyin fails
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
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_skips_already_published(mock_client_cls, mock_get_settings):
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

    svc = PublishService(db, MagicMock(), MagicMock())
    result = svc.publish(uuid.uuid4())

    assert result.results["bilibili"]["status"] == "skipped"
    mock_client_cls.assert_not_called()
