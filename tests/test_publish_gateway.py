from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

import pytest

from videoroll.apps.orchestrator_api.services.publishing_service import build_publish_gateway_request
from videoroll.apps.publish_gateway import normalize_social_publish_meta, publish_backend_url, publish_meta_key
from videoroll.apps.publish_request_builder import build_publish_gateway_request as build_queued_publish_gateway_request
from videoroll.apps.publish_request_builder import prepare_bilibili_publish_meta
from videoroll.db.models import SourceLicense, SourceType, Task


def test_build_publish_gateway_request_defaults_to_bilibili_and_platform_options() -> None:
    task = Task(id=uuid.uuid4(), source_type=SourceType.youtube, source_license=SourceLicense.own)
    payload = Mock(
        platform=None,
        account_id="acct-1",
        video_key="final/video.mp4",
        cover_key="cover.jpg",
        typeid_mode="ai_summary",
        meta={"title": "demo"},
        platform_options={"bilibili": {"typeid_mode": "meta"}, "douyin": {"draft": True}},
    )

    with patch(
        "videoroll.apps.orchestrator_api.services.publishing_service.prepare_publish_meta",
        return_value={"title": "prepared"},
    ):
        req = build_publish_gateway_request(
            task=task,
            task_id=task.id,
            payload=payload,
            video_key="final/video.mp4",
            db=object(),  # type: ignore[arg-type]
            store=object(),  # type: ignore[arg-type]
        )

    assert req["platform"] == "bilibili"
    assert req["meta"] == {"title": "prepared"}
    assert req["typeid_mode"] == "meta"
    assert req["platform_options"] == {"typeid_mode": "meta"}


def test_publish_backend_url_routes_supported_platforms() -> None:
    settings = Mock(bilibili_publisher_url="http://bili", social_publisher_url="http://social")

    assert publish_backend_url(settings, "bilibili") == "http://bili/bilibili/publish"
    assert publish_backend_url(settings, "douyin") == "http://social/sau/douyin/publish"
    assert publish_backend_url(settings, "xiaohongshu") == "http://social/sau/xiaohongshu/publish"
    assert publish_backend_url(settings, "kuaishou") == "http://social/sau/kuaishou/publish"


def test_publish_backend_url_rejects_unknown_platform() -> None:
    settings = Mock(bilibili_publisher_url="http://bili", social_publisher_url="http://social")

    with pytest.raises(ValueError, match="unsupported publish platform"):
        publish_backend_url(settings, "threads")


def test_social_publish_meta_does_not_require_bilibili_typeid() -> None:
    assert normalize_social_publish_meta(
        {"title": " Demo ", "description": " Body ", "tags": ["#one", "one", "two"]},
        "kuaishou",
    ) == {"title": "Demo", "desc": "Body", "tags": ["one", "two"]}


def test_douyin_meta_uses_short_author_credit_and_four_non_videoroll_topics() -> None:
    assert normalize_social_publish_meta(
        {
            "title": "【熟肉】翻译标题 - Original Creator",
            "description": "原视频：https://example.test/watch\n博主：Original Creator\n\n很长的 B 站简介",
            "tags": ["videoroll", "#AI", "AI", "科技", "资讯", "剪辑", "额外"],
        },
        "douyin",
        original_author="Original Creator",
    ) == {
        "title": "翻译标题",
        "desc": "原作者：Original Creator",
        "tags": ["AI", "科技", "资讯", "剪辑"],
    }


def test_douyin_meta_can_recover_author_from_existing_credit() -> None:
    assert normalize_social_publish_meta(
        {"title": "翻译标题 - Original Creator", "desc": "原作者：Original Creator", "tags": []},
        "douyin",
    ) == {"title": "翻译标题", "desc": "原作者：Original Creator", "tags": []}


def test_direct_douyin_publish_uses_stored_youtube_uploader() -> None:
    task = Task(id=uuid.uuid4(), source_type=SourceType.youtube, source_license=SourceLicense.own)
    payload = Mock(
        platform="douyin",
        account_id=str(uuid.uuid4()),
        cover_key=None,
        typeid_mode=None,
        force_retry=False,
        meta={"title": "翻译标题", "description": "B 站长简介", "tags": ["AI"]},
        platform_options={},
    )
    db = Mock()
    db.get.return_value = Mock(uploader="Original Creator")

    request = build_publish_gateway_request(
        task=task,
        task_id=task.id,
        payload=payload,
        video_key="final/video.mp4",
        db=db,
        store=object(),  # type: ignore[arg-type]
    )

    assert request["meta"]["desc"] == "原作者：Original Creator"


def test_queued_douyin_publish_uses_stored_youtube_uploader() -> None:
    task = Task(id=uuid.uuid4(), source_type=SourceType.youtube, source_license=SourceLicense.own)
    db = Mock()
    db.get.return_value = Mock(uploader="Original Creator")

    request = build_queued_publish_gateway_request(
        task=task,
        task_id=task.id,
        payload={
            "platform": "douyin",
            "account_id": str(uuid.uuid4()),
            "meta": {"title": "翻译标题", "description": "B 站长简介", "tags": ["AI"]},
        },
        video_key="final/video.mp4",
        db=db,
        store=object(),  # type: ignore[arg-type]
    )

    assert request["meta"]["desc"] == "原作者：Original Creator"


@pytest.mark.parametrize(
    ("suggested", "explicit", "expected"),
    [
        ([], ["视频分享"], ["视频分享"]),
        (["videoroll", "科技", "科技"], ["视频分享"], ["科技", "视频分享"]),
        (["VideoRoll", "科技"], ["videoroll", "视频分享"], ["科技", "videoroll", "视频分享"]),
    ],
)
def test_queued_bilibili_publish_never_injects_brand_tag(
    suggested: list[str], explicit: list[str], expected: list[str],
) -> None:
    task = Task(
        id=uuid.uuid4(), source_type=SourceType.youtube,
        source_license=SourceLicense.own,
    )
    meta = {"title": "实测投稿", "desc": "描述", "tags": explicit, "typeid": 17}
    db = Mock()

    with patch("videoroll.apps.publish_request_builder.get_task_bilibili_tags", return_value=suggested):
        direct = prepare_bilibili_publish_meta(
            task=task, payload_meta=meta, db=db, store=object(),  # type: ignore[arg-type]
        )
        queued = build_queued_publish_gateway_request(
            task=task, task_id=task.id,
            payload={"platform": "bilibili", "account_id": "acct", "meta": meta},
            video_key="final/video.mp4",
            db=db, store=object(),  # type: ignore[arg-type]
        )

    assert direct["tags"] == expected
    assert queued["meta"]["tags"] == expected
    # Builders must not mutate metadata already prepared for other targets.
    assert meta["tags"] == explicit


def test_queued_bilibili_publish_limits_tags_without_preemptive_brand() -> None:
    task = Task(
        id=uuid.uuid4(), source_type=SourceType.youtube,
        source_license=SourceLicense.own,
    )
    with patch("videoroll.apps.publish_request_builder.get_task_bilibili_tags", return_value=[
        "videoroll", *(f"标签{i}" for i in range(12)),
    ]):
        meta = prepare_bilibili_publish_meta(
            task=task,
            payload_meta={"title": "实测投稿", "tags": ["视频分享"], "typeid": 17},
            db=Mock(), store=object(),  # type: ignore[arg-type]
        )
    assert meta["tags"] == [f"标签{i}" for i in range(10)]
    assert "videoroll" not in [tag.casefold() for tag in meta["tags"]]


def test_xiaohongshu_publish_meta_rejects_more_than_ten_tags() -> None:
    with pytest.raises(ValueError, match="at most 10 tags"):
        normalize_social_publish_meta(
            {"title": "demo", "tags": [f"tag-{index}" for index in range(11)]},
            "xiaohongshu",
        )


def test_publish_meta_key_is_scoped_by_platform() -> None:
    task_id = uuid.uuid4()
    assert publish_meta_key(task_id, "douyin") == f"meta/{task_id}/publish/douyin.json"
