from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import httpx
import pytest

from videoroll.apps.bilibili_publisher.bilibili_web_client import (
    BilibiliWebClient, BilibiliWebError, PreuploadInfo, UploadMeta,
)


def _client(chunk_size=2):
    client = BilibiliWebClient("SESSDATA=test")
    client.preupload_video = Mock(return_value=PreuploadInfo(
        auth="test-auth", biz_id=123, chunk_size=chunk_size,
        endpoint="//upos.example.test", upos_uri="upos://bucket/video.mp4"))
    client.post_video_meta = Mock(return_value=UploadMeta(
        upload_id="parallel-1", bucket="bucket", key="video.mp4"))
    client._upos = Mock()
    client._upos.post.return_value = httpx.Response(200, json={"OK": 1})
    return client


def test_bilibili_upload_runs_multiple_chunks_concurrently_with_sorted_etags(tmp_path, monkeypatch):
    monkeypatch.setenv("BILIBILI_UPLOAD_WORKERS", "3")
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"abcdefghij")  # 5 parts, 2 bytes each
    client = _client()
    barrier = threading.Barrier(3, timeout=3)
    called = []

    def upload(*args, params, content, **kwargs):
        number = int(params["partNumber"])
        called.append(number)
        if number <= 3:
            barrier.wait()  # Sequential implementation times out here.
            time.sleep((4 - number) * 0.02)
        assert content == b"abcdefghij"[(number - 1) * 2:number * 2]
        return httpx.Response(200, headers={"ETag": f"etag-{number}"})

    client._upos.put.side_effect = upload
    updates = []
    try:
        _upload, report = client.upload_video_file(
            path, on_progress=lambda done, total: updates.append((done, total)))
    finally:
        client.close()
    assert len(called) == 5
    assert updates[-1] == (10, 10)
    assert sorted(done for done, total in updates) == [2, 4, 6, 8, 10]
    submitted = client._upos.post.call_args.kwargs["json"]["parts"]
    assert [part["partNumber"] for part in submitted] == [1, 2, 3, 4, 5]
    assert [part["eTag"] for part in submitted] == [f"etag-{n}" for n in range(1, 6)]
    assert report["chunks"] == 5


def test_parallel_upload_failure_never_finalizes_incomplete_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("BILIBILI_UPLOAD_WORKERS", "4")
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"abcdefghij")
    client = _client()

    def upload(*args, params, **kwargs):
        if params["partNumber"] == "3":
            return httpx.Response(503, text="temporary UPOS error")
        return httpx.Response(200)

    client._upos.put.side_effect = upload
    try:
        with pytest.raises(BilibiliWebError, match="chunk 3 upload failed"):
            client.upload_video_file(path)
        client._upos.post.assert_not_called()
    finally:
        client.close()


def test_bilibili_upload_workers_one_preserves_serial_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("BILIBILI_UPLOAD_WORKERS", "1")
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"abcdefghij")
    client = _client()
    order = []

    def upload(*args, params, **kwargs):
        order.append(int(params["partNumber"]))
        return httpx.Response(200)

    client._upos.put.side_effect = upload
    try:
        client.upload_video_file(path)
    finally:
        client.close()
    assert order == [1, 2, 3, 4, 5]
