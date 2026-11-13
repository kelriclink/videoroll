from __future__ import annotations

from videoroll.apps.bilibili_publisher.activity_selector import select_publish_activity
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta


def _meta(**overrides):
    values = dict(title="机械键盘测试", desc="介绍机械键盘的结构",
                  typeid=17, tags=["机械键盘"], copyright=1)
    values.update(overrides)
    return BilibiliPublishMeta.model_validate(values)


class _Client:
    def __init__(self):
        self.recommended = []
        self.search = {}
        self.public = []
        self.details = {}
        self.calls = []

    def recommended_topics(self, **kw):
        self.calls.append(("recommended", kw))
        return self.recommended

    def search_topics(self, keyword):
        self.calls.append(("search", keyword))
        return self.search.get(keyword, [])

    def public_activities(self, *, page=1):
        self.calls.append(("public", page))
        return self.public if page == 1 else []

    def public_activity_detail(self, activity_id):
        self.calls.append(("detail", activity_id))
        return self.details[activity_id]


class _AI:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request_json(self, **kw):
        self.calls.append(kw)
        return self.answers.pop(0)


def test_default_no_activity_when_empty():
    client = _Client()
    ai = _AI()
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["candidate_count"] == 0
    assert ai.calls == []


def test_pick_verified_topic_and_ignore_model_supplied_ids():
    client = _Client()
    client.recommended = [{
        "topic_id": 12, "mission_id": 34, "topic_name": "机械键盘大赛",
        "description": "投稿机械键盘相关原创视频", "activity_text": "有奖活动",
    }]
    client.search["机械键盘大赛"] = [{
        "id": 12, "mission_id": 34, "name": "机械键盘大赛",
        "act_protocol": "机械键盘原创视频活动",
    }]
    ai = _AI({"refs": ["topic:12", "topic:999"]},
             {"ref": "topic:12", "mission_id": 99999, "topic_id": 77777, "reason": "符合主题"})
    changed, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert (changed.topic_id, changed.mission_id) == (12, 34)
    assert audit["selected"] is True
    assert len(ai.calls) == 2
    assert ai.calls[0]["purpose"] == "bilibili_activity_shortlist"
    assert ai.calls[1]["purpose"] == "bilibili_activity_final"
    assert ai.calls[0]["network_retries"] == 0


def test_ai_cannot_hallucinate_or_choose_unknown_topic():
    client = _Client()
    client.recommended = [{"topic_id": 12, "mission_id": 34, "topic_name": "机械键盘大赛"}]
    ai = _AI({"refs": ["topic:999999"]})
    changed, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert changed.topic_id is None
    assert not audit["selected"]
    assert len(ai.calls) == 1


def test_public_activity_id_must_be_mapped_to_creator_topic():
    client = _Client()
    client.public = [{
        "id": 456, "name": "机械键盘大赛", "stime": 50, "etime": 200,
        "desc": "自制视频投稿",
    }]
    client.details[456] = {
        "id": 456, "name": "机械键盘大赛", "stime": 50, "etime": 200,
        "dic": "参加原创机械键盘视频活动",
    }
    client.search["机械键盘大赛"] = [{
        "id": 12, "mission_id": 34, "name": "机械键盘大赛",
        "act_protocol": "原创视频",
    }]
    ai = _AI({"refs": ["public:456"]}, {"ref": "public:456", "reason": "匹配"})
    updated, _audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert (updated.topic_id, updated.mission_id) == (12, 34)
    assert updated.mission_id != 456


def test_expired_public_activity_is_not_sent_to_ai():
    client = _Client()
    client.public = [{"id": 456, "name": "已结束活动", "stime": 20, "etime": 90}]
    ai = _AI()
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["candidate_count"] == 0


def test_cannot_auto_join_reprint_without_explicit_rule():
    client = _Client()
    client.recommended = [{"topic_id": 12, "mission_id": 34, "topic_name": "机械键盘大赛"}]
    client.search["机械键盘大赛"] = [{
        "id": 12, "mission_id": 34, "name": "机械键盘大赛",
        "act_protocol": "仅限原创",
    }]
    ai = _AI({"refs": ["topic:12"]})
    updated, audit = select_publish_activity(client, ai, _meta(copyright=2, source="https://example.com/"),
                                             tid=17, now=100)
    assert updated.topic_id is None
    assert audit["reason"] == "no verified eligible activity detail"


def test_disallow_reprint_does_not_match_allow_substring():
    client = _Client()
    client.recommended = [{"topic_id": 12, "mission_id": 34, "topic_name": "机械键盘大赛"}]
    client.search["机械键盘大赛"] = [{
        "id": 12, "mission_id": 34, "name": "机械键盘大赛",
        "act_protocol": "不允许转载",
    }]
    ai = _AI({"refs": ["topic:12"]})
    updated, audit = select_publish_activity(client, ai, _meta(copyright=2, source="https://example.com/"),
                                             tid=17, now=100)
    assert updated.topic_id is None
    assert not audit["selected"]


def test_original_mission_must_be_confirmed_by_detail():
    client = _Client()
    client.recommended = [{"topic_id": 12, "mission_id": 34, "topic_name": "机械键盘大赛"}]
    client.search["机械键盘大赛"] = [{"id": 12, "mission_id": 0, "name": "机械键盘大赛"}]
    ai = _AI({"refs": ["topic:12"]})
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["detail_checked_count"] == 0


def test_manual_activity_is_not_overwritten():
    client = _Client()
    ai = _AI()
    original = _meta(topic_id=12, mission_id=34)
    changed, audit = select_publish_activity(client, ai, original, tid=17)
    assert changed is original
    assert "manual" in audit["reason"]
    assert not client.calls
