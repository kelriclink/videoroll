"""Tests for videoroll.apps.publish_service.PublishAllResult."""

from videoroll.apps.publish_service import PublishAllResult


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
