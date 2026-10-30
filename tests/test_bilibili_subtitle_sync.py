from pathlib import Path
from unittest.mock import Mock
import pytest
from videoroll.apps.bilibili_publisher.subtitle_sync import _cids, _upload
from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebError

def test_pages_match_p1_p2_order():
    assert _cids({"pages": [{"page": 1, "cid": 123}, {"page": 2, "cid": 456}]}) == [123, 456]

@pytest.mark.parametrize("pages", [[], [{"cid": 5}, {"cid": 5}], [{"cid": 0}], [{}]])
def test_bad_pages_cannot_upload(pages):
    with pytest.raises(BilibiliWebError):
        _cids({"pages": pages})

def test_upload_srt_and_presave_uses_cid(tmp_path: Path):
    path = tmp_path / "zh.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\n中文\n", encoding="utf-8")
    client = Mock()
    client._bili.post.side_effect = [
        Mock(status_code=200, json=lambda: {"code": 0, "data": {"location": "bfs://subtitle/test"}}),
        Mock(status_code=200, json=lambda: {"code": 0}),
    ]
    assert _upload(client, path, "csrf", 100, 200) == "bfs://subtitle/test"
    assert client._bili.post.call_count == 2
    first, second = client._bili.post.call_args_list
    assert first.args[0].endswith("/x/upload/web/image")
    assert second.args[0].endswith("/x/v2/dm/subtitle/draft/preSave")
    assert second.kwargs["data"]["aid"] == "100"
    assert second.kwargs["data"]["oid"] == "200"
    assert '"zh-Hans"' in second.kwargs["data"]["files"]

def test_presave_failure_is_not_success(tmp_path):
    path = tmp_path / "zh.srt"
    path.write_text("subtitle", encoding="utf-8")
    client = Mock()
    client._bili.post.side_effect = [
        Mock(status_code=200, json=lambda: {"code": 0, "data": {"location": "bfs://test"}}),
        Mock(status_code=200, json=lambda: {"code": -400, "message": "invalid"}),
    ]
    with pytest.raises(BilibiliWebError):
        _upload(client, path, "csrf", 100, 200)
