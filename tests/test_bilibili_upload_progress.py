from __future__ import annotations

from unittest.mock import Mock

import httpx

from videoroll.apps.bilibili_publisher.bilibili_web_client import (
    BilibiliWebClient,
    PreuploadInfo,
    UploadMeta,
)


def test_upload_video_file_reports_completed_bytes_after_each_uploaded_chunk(tmp_path) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"abcde")
    client = BilibiliWebClient("SESSDATA=test")
    client.preupload_video = Mock(
        return_value=PreuploadInfo(
            auth="upload-auth",
            biz_id=123,
            chunk_size=2,
            endpoint="//upos.example.test",
            upos_uri="upos://bucket/video.mp4",
        )
    )
    client.post_video_meta = Mock(return_value=UploadMeta(upload_id="upload-1", bucket="bucket", key="video.mp4"))
    client._upos = Mock()
    client._upos.put.side_effect = [
        httpx.Response(200, headers={"ETag": "part-1"}),
        httpx.Response(200, headers={"ETag": "part-2"}),
        httpx.Response(200, headers={"ETag": "part-3"}),
    ]
    client._upos.post.return_value = httpx.Response(200, json={"OK": 1})
    updates: list[tuple[int, int]] = []

    try:
        _uploaded, debug = client.upload_video_file(video_path, on_progress=lambda uploaded, total: updates.append((uploaded, total)))
    finally:
        client.close()

    assert updates == [(2, 5), (4, 5), (5, 5)]
    assert debug["chunks"] == 3
