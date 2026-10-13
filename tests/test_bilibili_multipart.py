from __future__ import annotations

import uuid
from unittest.mock import Mock

import pytest

from videoroll.apps.bilibili_publisher import worker
from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient, BilibiliWebError, UploadedVideo
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta, PublishRequest


def _meta() -> BilibiliPublishMeta:
    return BilibiliPublishMeta(title="双分P投稿", typeid=171, tags=["videoroll"])


def _parts() -> list[dict]:
    return [
        {"video": {"type": "storage", "key": "render/task/subtitle.mp4"}, "title": "P1 中文字幕版"},
        {"video": {"type": "storage", "key": "render/task/dubbed.mp4"}, "title": "P2 中文配音版"},
    ]


def test_publish_request_accepts_atomic_two_parts() -> None:
    req = PublishRequest(
        task_id=uuid.uuid4(),
        video=_parts()[0]["video"],
        video_parts=_parts(),
        meta=_meta(),
    )
    assert [part.title for part in req.video_parts] == ["P1 中文字幕版", "P2 中文配音版"]


@pytest.mark.parametrize("parts", [
    [_parts()[0]],
    [_parts()[1], _parts()[0]],
    [_parts()[0], {**_parts()[1], "video": _parts()[0]["video"]}],
])
def test_publish_request_refuses_invalid_two_part_contract(parts: list[dict]) -> None:
    with pytest.raises(ValueError):
        PublishRequest(task_id=uuid.uuid4(), video=_parts()[0]["video"], video_parts=parts, meta=_meta())


def test_worker_extracts_video_parts_in_order() -> None:
    assert worker._video_parts_for_job(
        {"video_parts": _parts()}, "render/task/subtitle.mp4", "default"
    ) == [
        ("render/task/subtitle.mp4", "P1 中文字幕版"),
        ("render/task/dubbed.mp4", "P2 中文配音版"),
    ]
    assert worker._video_parts_for_job({}, "one.mp4", "单视频") == [("one.mp4", "单视频")]
    with pytest.raises(ValueError):
        worker._video_parts_for_job(
            {"video_parts": [_parts()[1], _parts()[0]]},
            "render/task/subtitle.mp4",
            "default",
        )


def test_web_submit_uses_one_archive_with_two_ordered_videos() -> None:
    client = BilibiliWebClient("SESSDATA=fake")
    client._bili = Mock()
    client._bili.post.return_value = Mock(status_code=200)
    client._bili.post.return_value.json.return_value = {"code": 0, "data": {"aid": 1, "bvid": "BVtest"}}
    first = UploadedVideo("uploaded_p1", 1001, "u1", "upos1")
    second = UploadedVideo("uploaded_p2", 1002, "u2", "upos2")
    try:
        result = client.add_archive(
            _meta(), csrf="test", tid=171, uploaded=first,
            uploaded_parts=[(first, "P1 中文字幕版"), (second, "P2 中文配音版")],
        )
        body = client._bili.post.call_args.kwargs["json"]
        assert result["code"] == 0
        assert [p["cid"] for p in body["videos"]] == [1001, 1002]
        assert [p["title"] for p in body["videos"]] == ["P1 中文字幕版", "P2 中文配音版"]
        client._bili.post.assert_called_once()
    finally:
        client.close()


def test_web_submit_rejects_duplicate_video_cid_before_http() -> None:
    client = BilibiliWebClient("SESSDATA=fake")
    client._bili = Mock()
    first = UploadedVideo("upload", 123, "u", "upos")
    try:
        with pytest.raises(BilibiliWebError):
            # The business layer can also be called directly: reject duplicate IDs.
            client.add_archive(
                _meta(), csrf="test", tid=171, uploaded=first,
                uploaded_parts=[(first, "P1"), (first, "P2")],
            )
        client._bili.post.assert_not_called()
    finally:
        client.close()
