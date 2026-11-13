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


def _public(client, *, start=50, end=200, state=1, rules="投稿机械键盘视频参加活动",
            mission_id=34, detail_state=None):
    client.public = [{
        "id": 456, "sid": 9001, "name": "机械键盘大赛", "stime": start, "etime": end,
        "state": state, "desc": "投稿机械键盘视频参加活动",
    }]
    client.details[9001] = {
        "id": 9001, "name": "机械键盘大赛", "stime": start, "etime": end,
        "state": state if detail_state is None else detail_state,
        "dic": rules,
    }
    client.search["机械键盘大赛"] = [{
        "id": 12, "mission_id": mission_id, "name": "机械键盘大赛",
        "act_protocol": rules,
    }]


def test_default_no_activity_when_empty():
    client = _Client()
    ai = _AI()
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.topic_id is None
    assert updated.mission_id is None
    assert audit["candidate_count"] == 0
    assert ai.calls == []


def test_verified_regular_topic_needs_no_mission_id():
    client = _Client()
    client.search["机械键盘"] = [{"id": 12, "name": "机械键盘", "mission_id": 0}]
    ai = _AI({"refs": ["topic:12"]}, {"ref": "topic:12", "reason": "相关"})
    changed, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert changed.topic_id == 12
    assert changed.mission_id is None
    assert audit["kind"] == "topic"
    assert audit["topic_verified_count"] == 1
    assert audit["activity_verified_count"] == 0
    assert len(ai.calls) == 2


def test_topic_with_mission_in_api_still_does_not_auto_join_activity():
    client = _Client()
    client.recommended = [{
        "topic_id": 12, "mission_id": 34, "topic_name": "机械键盘大赛",
        "description": "投稿机械键盘相关视频",
    }]
    client.search["机械键盘大赛"] = [{
        "id": 12, "mission_id": 34, "name": "机械键盘大赛",
        "act_protocol": "机械键盘原创视频活动",
    }]
    ai = _AI({"refs": ["topic:12", "topic:999"]},
             {"ref": "topic:12", "mission_id": 999, "topic_id": 777, "reason": "符合主题"})
    changed, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert (changed.topic_id, changed.mission_id) == (12, None)
    assert audit["selected"] is True
    assert audit["kind"] == "topic"
    assert ai.calls[0]["network_retries"] == 0


def test_ai_cannot_hallucinate_or_choose_unknown_topic():
    client = _Client()
    client.recommended = [{"topic_id": 12, "topic_name": "机械键盘大赛"}]
    ai = _AI({"refs": ["topic:999999"]})
    changed, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert changed.topic_id is None
    assert not audit["selected"]
    assert len(ai.calls) == 1


def test_topic_must_match_confirmed_id_and_name():
    client = _Client()
    client.recommended = [{"topic_id": 12, "topic_name": "机械键盘大赛"}]
    client.search["机械键盘大赛"] = [{"id": 99, "name": "机械键盘大赛"}]
    ai = _AI({"refs": ["topic:12"]})
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.topic_id is None
    assert audit["detail_checked_count"] == 0
    assert audit["rejected_refs"]["topic:12"]


def test_public_activity_needs_verified_real_mission_and_full_time_window():
    client = _Client()
    _public(client)
    ai = _AI({"refs": ["public:456"]}, {"ref": "public:456", "reason": "匹配"})
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert (updated.topic_id, updated.mission_id) == (12, 34)
    assert updated.mission_id != 456
    assert audit["kind"] == "activity"
    assert audit["activity_verified_count"] == 1


def test_public_list_id_cannot_be_guessed_as_subject_sid():
    client = _Client()
    _public(client)
    client.public[0].pop("sid")
    ai = _AI({"refs": ["public:456"]})
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["activity_verified_count"] == 0
    assert not any(method == "detail" for method, _ in client.calls)


def test_activity_missing_real_mission_is_rejected():
    client = _Client()
    _public(client, mission_id=0)
    ai = _AI({"refs": ["public:456"]})
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert (updated.topic_id, updated.mission_id) == (None, None)
    assert audit["detail_checked_count"] == 0


def test_expired_public_activity_is_not_sent_to_ai():
    client = _Client()
    _public(client, start=20, end=90)
    ai = _AI()
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["candidate_count"] == 0


def test_future_or_missing_time_window_is_not_eligible():
    for start, end in ((101, 200), (0, 200), (50, 0)):
        client = _Client()
        _public(client, start=start, end=end)
        ai = _AI()
        updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
        assert updated.mission_id is None
        assert audit["candidate_count"] == 0


def test_public_activity_detail_time_or_state_mismatch_rejected():
    client = _Client()
    _public(client)
    client.details[9001]["etime"] = 201
    ai = _AI({"refs": ["public:456"]})
    updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["detail_checked_count"] == 0

    client = _Client()
    _public(client, detail_state=0)
    ai = _AI({"refs": ["public:456"]})
    updated, _ = select_publish_activity(client, ai, _meta(), tid=17, now=100)
    assert updated.mission_id is None


def test_reprint_cannot_join_original_only_activity():
    client = _Client()
    _public(client, rules="本活动仅限原创稿件")
    ai = _AI({"refs": ["public:456"]})
    updated, audit = select_publish_activity(
        client, ai, _meta(copyright=2, source="https://example.com/"), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["activity_verified_count"] == 0


def test_external_youtube_source_cannot_evade_reprint_gate_by_self_made_flag():
    client = _Client()
    _public(client, rules="仅限原创投稿")
    ai = _AI({"refs": ["public:456"]})
    updated, audit = select_publish_activity(
        client, ai, _meta(copyright=1), tid=17, now=100, source_is_external=True)
    assert updated.mission_id is None
    assert audit["rejected_refs"]["public:456"]


def test_public_activity_explicitly_allows_reprints():
    client = _Client()
    _public(client, rules="本活动允许转载作品参与，请附原视频出处")
    ai = _AI({"refs": ["public:456"]}, {"ref": "public:456", "reason": "允许转载"})
    updated, audit = select_publish_activity(
        client, ai, _meta(copyright=2, source="https://example.com/"), tid=17, now=100)
    assert (updated.topic_id, updated.mission_id) == (12, 34)
    assert audit["kind"] == "activity"


def test_reprint_permission_cannot_override_ban_on_copied_content():
    client = _Client()
    _public(client, rules="允许转载作品参与，但严禁搬运其他UP主的视频")
    ai = _AI({"refs": ["public:456"]})
    updated, audit = select_publish_activity(
        client, ai, _meta(copyright=2, source="https://example.com/"), tid=17, now=100)
    assert updated.mission_id is None
    assert audit["activity_verified_count"] == 0


def test_activity_registration_and_account_requirements_fail_closed():
    for restriction in ("需先报名审核", "仅限受邀UP主", "粉丝数1000以上"):
        client = _Client()
        _public(client, rules=f"投稿参加活动；{restriction}")
        ai = _AI({"refs": ["public:456"]})
        updated, audit = select_publish_activity(client, ai, _meta(), tid=17, now=100)
        assert updated.mission_id is None
        assert audit["activity_verified_count"] == 0


def test_no_ai_choice_keeps_original_meta():
    client = _Client()
    client.search["机械键盘"] = [{"id": 12, "name": "机械键盘"}]
    ai = _AI({"refs": ["topic:12"]}, {"ref": "none", "reason": "不相关"})
    original = _meta()
    updated, audit = select_publish_activity(client, ai, original, tid=17, now=100)
    assert updated is original
    assert not audit["selected"]


def test_manual_topic_is_preserved_without_any_api_calls():
    client = _Client()
    ai = _AI()
    original = _meta(topic_id=12)
    updated, audit = select_publish_activity(client, ai, original, tid=17)
    assert updated is original
    assert "manual" in audit["reason"]
    assert client.calls == []


def test_manual_activity_is_preserved_without_any_api_calls():
    client = _Client()
    ai = _AI()
    original = _meta(topic_id=12, mission_id=34)
    changed, audit = select_publish_activity(client, ai, original, tid=17)
    assert changed is original
    assert "manual" in audit["reason"]
    assert not client.calls


def test_old_videoroll_tag_is_not_used_to_search_topics():
    client = _Client()
    ai = _AI()
    select_publish_activity(client, ai, _meta(tags=["videoroll", "机械键盘"]), tid=17, now=100)
    searched = [arg for method, arg in client.calls if method == "search"]
    assert "videoroll" not in [keyword.lower() for keyword in searched]
    assert "机械键盘" in searched
