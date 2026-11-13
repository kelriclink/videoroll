from __future__ import annotations

import pytest

from videoroll.apps.orchestrator_api.services import bilibili_discovery as module


def ranking_payload():
    return {"code": 0, "data": {"list": [
        {"bvid": "BV1baWq6JED5", "title": "USB-C 评测",
         "owner": {"name": "数码 UP 主"}, "tname": "数码", "pic": "https://i0.hdslb.com/bfs/archive/a.jpg",
         "stat": {"view": 2524111, "like": 276301, "danmaku": 500}},
        {"bvid": "BV1baWq6JED5", "title": "重复"},
        {"bvid": "../unsafe", "title": "无效"},
    ]}}


def activity_payload():
    return {"code": 0, "data": {"list": [
        {"id": 2771, "name": "明日方舟创作活动", "state": 1, "stime": 50, "etime": 200,
         "desc": "公开活动", "pc_url": "https://www.bilibili.com/blackboard/era/example.html",
         "pc_cover": "https://i0.hdslb.com/bfs/activity/cover.jpg"},
        {"id": 2772, "name": "已结束活动", "state": 1, "stime": 1, "etime": 99,
         "pc_url": "https://www.bilibili.com/blackboard/era/expired.html"},
        {"id": 2773, "name": "外站恶意活动", "state": 1, "stime": 50, "etime": 200,
         "pc_url": "https://evil.example/blackboard/era/x"},
        {"id": 2774, "name": "没有明确结束时间", "state": 1, "stime": 50, "etime": 0,
         "pc_url": "https://www.bilibili.com/blackboard/era/x"},
        {"id": 2775, "name": "未来活动", "state": 1, "stime": 101, "etime": 300,
         "pc_url": "https://www.bilibili.com/blackboard/era/future"},
    ]}}


def test_ranking_normalizes_and_deduplicates_urls():
    result = module.parse_ranking(ranking_payload())
    assert len(result) == 1
    assert result[0]["rank"] == 1
    assert result[0]["url"] == "https://www.bilibili.com/video/BV1baWq6JED5"
    assert result[0]["views"] == 2524111
    assert result[0]["uploader"] == "数码 UP 主"
    assert result[0]["cover"].startswith("https://i0.hdslb.com")


def test_activity_shows_only_current_and_official_links():
    result = module.parse_activities(activity_payload(), now=100)
    assert len(result) == 1
    assert result[0]["id"] == 2771
    assert result[0]["eligibility"] == "unknown"
    assert "mission_id" not in result[0]
    assert result[0]["url"].startswith("https://www.bilibili.com/blackboard/")


@pytest.mark.parametrize("bad", [{"code": -352}, {"code": 0}, {"code": 0, "data": {"list": "oops"}}])
def test_invalid_upstream_response_is_rejected(bad):
    with pytest.raises(ValueError):
        module.parse_ranking(bad)
    with pytest.raises(ValueError):
        module.parse_activities(bad, now=100)


def test_unrepresentable_activity_timestamp_skipped():
    payload = activity_payload()
    payload["data"]["list"][0]["etime"] = 10**15
    assert module.parse_activities(payload, now=100) == []


def test_malformed_bvid_or_image_does_not_reach_page():
    payload = ranking_payload()
    payload["data"]["list"][0]["pic"] = "http://127.0.0.1/secret"
    item = module.parse_ranking(payload)[0]
    assert item["cover"] is None
    assert module._public_link("https://evil.example/blackboard/a") is None
    assert module._public_link("http://www.bilibili.com/blackboard/a") is None


def test_cache_and_manual_refresh_are_bounded(monkeypatch):
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    module._CACHE.clear()
    calls = []
    def fetch(url, params):
        calls.append(url)
        return ranking_payload() if "ranking" in url else activity_payload()
    monkeypatch.setattr(module, "_upstream", fetch)
    try:
        first = module.get_bilibili_discovery()
        assert len(calls) == 3
        second = module.get_bilibili_discovery(refresh=True)
        assert len(calls) == 3  # refresh is throttled
        assert first["ranking"]["items"] == second["ranking"]["items"]
        monkeypatch.setattr(module.time, "time", lambda: 1100.0)
        module.get_bilibili_discovery(refresh=True)
        assert len(calls) == 6
    finally:
        module._CACHE.clear()


def test_second_activity_page_failure_keeps_first_page(monkeypatch):
    def fetch(url, params):
        if params.get("pn") == 2:
            raise ValueError("second page unavailable")
        return activity_payload()
    monkeypatch.setattr(module, "_upstream", fetch)
    data = module._fetch_public_activities(now=100)
    assert len(data) == 1


def test_activity_pages_merge_and_deduplicate(monkeypatch):
    def fetch(url, params):
        first = activity_payload()
        if params.get("pn") == 2:
            first["data"]["list"][0] = {
                **first["data"]["list"][0], "id": 2776, "name": "第二页活动"
            }
        return first
    monkeypatch.setattr(module, "_upstream", fetch)
    data = module._fetch_public_activities(now=100)
    assert [item["id"] for item in data] == [2771, 2776]


def test_ranking_error_uses_labeled_popular_fallback(monkeypatch):
    module._CACHE.clear()
    calls = []
    def fetch(url, params):
        calls.append(url)
        if "ranking" in url:
            return {"code": -352, "message": "-352"}
        return ranking_payload() if "popular" in url else activity_payload()
    monkeypatch.setattr(module, "_upstream", fetch)
    try:
        snap = module.get_bilibili_discovery()
        assert snap["ranking"]["source"] == "热门综合"
        assert len(snap["ranking"]["items"]) == 1
        assert any("popular" in url for url in calls)
    finally:
        module._CACHE.clear()


def test_independent_partial_failures_and_stale_snapshot(monkeypatch):
    module._CACHE.clear()
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    def good(url, params):
        return ranking_payload() if "ranking" in url else activity_payload()
    monkeypatch.setattr(module, "_upstream", good)
    module.get_bilibili_discovery()
    monkeypatch.setattr(module.time, "time", lambda: 1400.0)
    def broken(url, params):
        if "ranking" in url or "popular" in url:
            raise ValueError("remote failed")
        return activity_payload()
    monkeypatch.setattr(module, "_upstream", broken)
    try:
        snapshot = module.get_bilibili_discovery()
        assert snapshot["ranking"]["stale"]
        assert snapshot["ranking"]["items"]
        assert snapshot["ranking"]["error"]
        assert not snapshot["activities"]["stale"]
        monkeypatch.setattr(module.time, "time", lambda: 6000.0)
        snapshot = module.get_bilibili_discovery()
        assert snapshot["ranking"]["items"] == []
        assert snapshot["ranking"]["error"]
    finally:
        module._CACHE.clear()
