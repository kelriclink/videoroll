from __future__ import annotations

from unittest.mock import Mock, patch
import uuid

import httpx
import pytest

from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient, BilibiliWebError
from videoroll.apps.bilibili_publisher.tag_recommendation import resolve_bilibili_publish_tags
from videoroll.apps.publish_request_builder import prepare_bilibili_publish_meta
from videoroll.apps.orchestrator_api.services.publishing_service import prepare_publish_meta
from videoroll.db.models import SourceLicense, SourceType, Task


def _resolve(generated=None, explicit=None, recommended=None, *, external_source=True):
    with patch("videoroll.apps.bilibili_publisher.tag_recommendation.get_bilibili_cookie_header", return_value="SESSDATA=stub") as cookie:
        with patch("videoroll.apps.bilibili_publisher.tag_recommendation.BilibiliWebClient") as factory:
            factory.return_value.__enter__.return_value.recommended_tags.return_value = recommended or []
            result = resolve_bilibili_publish_tags(
                db=Mock(), generated=generated or [], explicit=explicit or [],
                title="USB-C 线材有何区别", description="讨论线材规格",
                typeid=95, external_source=external_source,
            )
    return result, cookie.call_count, factory.return_value.__enter__.return_value.recommended_tags.call_count


def test_ai_generated_tags_win_without_upstream_call():
    result, credential_lookups, calls = _resolve(
        generated=["videoroll", "USB-C", "快充"], recommended=["数据线"]
    )
    assert result == ["USB-C", "快充"]
    assert (credential_lookups, calls) == (0, 0)


def test_external_ai_cannot_mark_reposted_video_as_original():
    result, credential_lookups, calls = _resolve(
        generated=["原创", "首发", "USB-C"], recommended=["数据线"]
    )
    assert result == ["USB-C"]
    assert (credential_lookups, calls) == (0, 0)


def test_manual_tag_also_prevents_official_fallback():
    result, credential_lookups, calls = _resolve(
        explicit=["用户自选", "videoroll"], recommended=["数据线"]
    )
    assert result == ["用户自选", "videoroll"]
    assert (credential_lookups, calls) == (0, 0)


def test_bilibili_recommendations_are_used_only_after_both_sources_empty():
    result, credential_lookups, calls = _resolve(
        recommended=["原创", "数据线", "区别", "videoroll", "数据线"]
    )
    assert result == ["数据线", "区别"]
    assert (credential_lookups, calls) == (1, 1)


def test_unrelated_generic_bilibili_tags_do_not_displace_topic_specific_tags():
    result, _, _ = _resolve(recommended=["生活记录", "记录", "数据线", "区别"])
    assert result == ["数据线", "区别"]


def test_only_generic_bilibili_suggestions_trigger_final_fallback():
    result, _, calls = _resolve(recommended=["生活记录", "记录", "原创"])
    assert result == ["视频分享"] and calls == 1


def test_bilibili_recommendations_can_keep_original_for_nonexternal_video():
    result, _, _ = _resolve(recommended=["原创", "CG"], external_source=False)
    assert result == ["原创", "CG"]


def test_generic_default_only_when_ai_and_bilibili_empty():
    result, _, calls = _resolve(recommended=[])
    assert result == ["视频分享"] and calls == 1


def test_missing_credentials_falls_back_without_network():
    with patch("videoroll.apps.bilibili_publisher.tag_recommendation.get_bilibili_cookie_header", return_value=""):
        with patch("videoroll.apps.bilibili_publisher.tag_recommendation.BilibiliWebClient") as factory:
            tags = resolve_bilibili_publish_tags(
                db=Mock(), generated=[], explicit=[], title="test",
                description="", typeid=17, external_source=True,
            )
    assert tags == ["视频分享"]
    factory.assert_not_called()


def test_official_api_failure_cannot_block_publish():
    with patch("videoroll.apps.bilibili_publisher.tag_recommendation.get_bilibili_cookie_header", return_value="stub"):
        with patch("videoroll.apps.bilibili_publisher.tag_recommendation.BilibiliWebClient") as factory:
            factory.return_value.__enter__.return_value.recommended_tags.side_effect = httpx.TimeoutException("timeout")
            tags = resolve_bilibili_publish_tags(
                db=Mock(), generated=[], explicit=[], title="test",
                description="", typeid=17, external_source=True,
            )
    assert tags == ["视频分享"]


@pytest.mark.parametrize("payload, expected", [
    ({"code": 0, "data": [{"tag": "科技"}, {"tag": "数据线"}, {"topic_id": 123}]}, ["科技", "数据线"]),
    ({"code": 0, "data": []}, []),
    ({"code": 0, "data": {"topics": [{"name": "不是真正标签"}]}}, []),
])
def test_creator_center_recommendation_parses_only_tag_records(payload, expected):
    with BilibiliWebClient("SESSDATA=fake") as client:
        client._bili = Mock()
        client._bili.get.return_value = Mock(json=lambda: payload, status_code=200)
        result = client.recommended_tags(type_id=17, title="demo", description="description")
        assert result == expected
        call = client._bili.get.call_args
        assert call.args[0] == "https://member.bilibili.com/x/vupre/web/tag/recommend"
        assert call.kwargs["params"]["title"] == "demo"
        assert call.kwargs["timeout"] <= 8


def test_creator_center_api_error_does_not_masquerade_as_valid_tags():
    with BilibiliWebClient("SESSDATA=fake") as client:
        client._bili = Mock()
        client._bili.get.return_value = Mock(json=lambda: {"code": -101, "data": []}, status_code=200)
        with pytest.raises(BilibiliWebError):
            client.recommended_tags(type_id=17, title="demo")


def test_real_queued_and_direct_builders_share_official_tag_resolution():
    task = Task(id=uuid.uuid4(), source_type=SourceType.youtube, source_license=SourceLicense.own)
    meta = {"title": "USB-C 数据线评测", "tags": [], "typeid": 95}
    db = Mock()
    with (
        patch("videoroll.apps.publish_request_builder.get_task_bilibili_tags", return_value=[]),
        patch("videoroll.apps.orchestrator_api.services.publishing_service.get_task_bilibili_tags", return_value=[]),
        patch("videoroll.apps.bilibili_publisher.tag_recommendation.get_bilibili_cookie_header", return_value="stub"),
        patch("videoroll.apps.bilibili_publisher.tag_recommendation.BilibiliWebClient") as factory,
    ):
        factory.return_value.__enter__.return_value.recommended_tags.return_value = ["数据线", "USB-C"]
        queued = prepare_bilibili_publish_meta(
            task=task, payload_meta=meta, db=db, store=object(),  # type: ignore[arg-type]
        )
        direct = prepare_publish_meta(
            task=task, payload_meta=meta, db=db, store=object(), allow_auto_draft=False,  # type: ignore[arg-type]
        )
    assert queued["tags"] == direct["tags"] == ["数据线", "USB-C"]
    assert meta["tags"] == []


def test_builders_both_fall_back_when_no_ai_or_official_tags():
    task = Task(id=uuid.uuid4(), source_type=SourceType.youtube, source_license=SourceLicense.own)
    meta = {"title": "USB-C 评测", "tags": [], "typeid": 95}
    db = Mock()
    with (
        patch("videoroll.apps.publish_request_builder.get_task_bilibili_tags", return_value=[]),
        patch("videoroll.apps.orchestrator_api.services.publishing_service.get_task_bilibili_tags", return_value=[]),
        patch("videoroll.apps.bilibili_publisher.tag_recommendation.get_bilibili_cookie_header", return_value=""),
    ):
        queued = prepare_bilibili_publish_meta(
            task=task, payload_meta=meta, db=db, store=object(),  # type: ignore[arg-type]
        )
        direct = prepare_publish_meta(
            task=task, payload_meta=meta, db=db, store=object(), allow_auto_draft=False,  # type: ignore[arg-type]
        )
    assert queued["tags"] == direct["tags"] == ["视频分享"]
