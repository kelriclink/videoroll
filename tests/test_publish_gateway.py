from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

import pytest

from videoroll.apps.orchestrator_api.services.publishing_service import build_publish_gateway_request
from videoroll.apps.publish_gateway import normalize_social_publish_meta, publish_backend_url, publish_meta_key
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
            s3=object(),  # type: ignore[arg-type]
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
        "douyin",
    ) == {"title": "Demo", "desc": "Body", "tags": ["one", "two"]}


def test_xiaohongshu_publish_meta_rejects_more_than_ten_tags() -> None:
    with pytest.raises(ValueError, match="at most 10 tags"):
        normalize_social_publish_meta(
            {"title": "demo", "tags": [f"tag-{index}" for index in range(11)]},
            "xiaohongshu",
        )


def test_publish_meta_key_is_scoped_by_platform() -> None:
    task_id = uuid.uuid4()
    assert publish_meta_key(task_id, "douyin") == f"meta/{task_id}/publish/douyin.json"
