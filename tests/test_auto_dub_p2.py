"""Auto YouDub P2 tests: no network traffic and never create a second BV."""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videoroll.apps.bilibili_publisher.auto_dub_p2 import (
    _upload_then_append, p2_title, schedule_after_p1,
)
from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient, UploadedVideo, BilibiliWebError
from videoroll.apps.subtitle_service.auto_profile_store import (
    AUTO_PROFILE_KEY, get_auto_profile, normalize_auto_dub_prefix,
)
from videoroll.db.models import (
    AppSetting, Asset, AssetKind, Base, DubbingJob, Platform,
    PublishBatch, PublishJob, PublishState, SourceLicense, SourceType,
    SubtitleJob, SubtitleJobStatus, Task, TaskStatus,
)


def _setup():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    task_id, pub_id, sub_id, dub_id = [uuid.uuid4() for _ in range(4)]
    with factory() as db:
        db.add(Task(
            id=task_id, source_type=SourceType.youtube,
            source_license=SourceLicense.own, status=TaskStatus.published,
            created_by="auto_youtube;auto_publish=;run_id=testrun",
        ))
        db.add(PublishBatch(id=uuid.uuid4(), task_id=task_id))
        db.flush()
        batch = db.query(PublishBatch).first()
        db.add(PublishJob(
            id=pub_id, task_id=task_id, batch_id=batch.id, state=PublishState.published,
            platform=Platform.bilibili, aid="98765", bvid="BV123",
            meta_json={"meta": {"title": "【熟肉】火箭引擎"}}, response_json={"mode": "web"},
        ))
        db.add(SubtitleJob(id=sub_id, task_id=task_id, status=SubtitleJobStatus.succeeded,
                           request_json={"artifacts": {"final_subtitle_segments_key": "srt.json"}}))
        db.commit()
    return factory, task_id, pub_id, sub_id, dub_id


def test_p2_prefix_replaces_only_auto_publish_prefix():
    assert p2_title("【熟肉】火箭引擎", "【熟肉】", "【中配】") == "【中配】火箭引擎"
    assert p2_title("火箭引擎", "【熟肉】", "配音") == "【配音】火箭引擎"
    assert normalize_auto_dub_prefix("【中配】") == "【中配】"
    assert normalize_auto_dub_prefix("中配") == "【中配】"
    with pytest.raises(ValueError):
        normalize_auto_dub_prefix("【长】乱【入】")


def test_optional_auto_p2_disabled_by_default():
    factory, _, pub_id, _, _ = _setup()
    with factory() as db:
        assert get_auto_profile(db)["auto_dub_p2"] is False
        job = db.get(PublishJob, pub_id)
        assert schedule_after_p1(db, job, job.task) is False
        assert db.query(AppSetting).filter(AppSetting.key == AUTO_PROFILE_KEY).count() == 0


def test_enabled_auto_p2_creates_durable_outbox_only_once():
    from videoroll.db.models import OutboxEvent
    factory, _, pub_id, _, _ = _setup()
    with factory() as db:
        db.add(AppSetting(key=AUTO_PROFILE_KEY, value_json={
            "auto_publish": True, "auto_publish_platforms": ["bilibili"],
            "auto_dub_p2": True, "auto_dub_p2_prefix": "【配音】",
            "publish_title_prefix": "【熟肉】",
        }))
        db.commit()
        job = db.get(PublishJob, pub_id)
        assert schedule_after_p1(db, job, job.task) is True
        db.commit()
        assert schedule_after_p1(db, job, job.task) is False
        record = (db.get(PublishJob, pub_id).response_json or {})["auto_dub_p2"]
        assert record["title"] == "【配音】火箭引擎"
        assert record["state"] == "queued"
        assert db.query(OutboxEvent).count() == 1
        assert db.get(PublishJob, pub_id).state == PublishState.published


class FakeClient:
    def __init__(self, *, fail=False, existing=False):
        self.calls = []
        self.fail = fail
        self.existing = existing

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_owned_archive(self, *, aid):
        self.calls.append(("view", aid))
        parts = [{"filename": "p1", "title": "原视频", "desc": "", "cid": 101}]
        if self.existing:
            parts.append({"filename": "p2", "title": "【中配】火箭引擎", "desc": "", "cid": 202})
        return {"archive": {"aid": aid, "title": "原视频", "copyright": 2,
                            "tid": 201, "tag": "科普", "desc": "原简介"}, "videos": parts}

    def upload_video_file(self, path):
        self.calls.append(("upload", str(path)))
        return UploadedVideo("p2", 202, "upload-id", "upos://p2"), {}

    def append_archive_part(self, **kwargs):
        self.calls.append(("edit", kwargs["aid"], kwargs["title"]))
        if self.fail:
            raise TimeoutError("Bilibili accepted or timed out")
        return {"code": 0}


def _success_setup():
    factory, task_id, pub_id, sub_id, dub_id = _setup()
    with factory() as db:
        job = db.get(PublishJob, pub_id)
        job.response_json = {**job.response_json, "auto_dub_p2": {
            "state": "dubbing", "title": "【中配】火箭引擎",
            "prefix": "【中配】", "dub_job_id": str(dub_id),
        }}
        db.add(DubbingJob(
            id=dub_id, task_id=task_id, subtitle_job_id=sub_id,
            operation_key="auto-dub-p2:unit", status="succeeded", progress=100,
            request_json={"model_path": "youdub:voxcpm2"},
            output_json={"video_key": "video/p2.mp4"},
        ))
        db.add(Asset(task_id=task_id, kind=AssetKind.video_dubbed,
                     storage_key="video/p2.mp4"))
        db.commit()
    return factory, pub_id


def test_append_p2_success_preserves_original_p1_and_bv():
    factory, pub_id = _success_setup()
    client = FakeClient()
    store = SimpleNamespace(path_for=lambda key: Path("/tmp") / key)
    with factory() as db:
        record = _upload_then_append(db, pub_id, client=client, store=store, csrf="dummy")
        assert record["state"] == "published"
        assert record["aid"] == "98765"
        assert [call[0] for call in client.calls] == ["view", "upload", "edit"]
        published = db.get(PublishJob, pub_id)
        assert published.state == PublishState.published and published.bvid == "BV123"
        # Reentry must never upload the same P2.
        _upload_then_append(db, pub_id, client=client, store=store, csrf="dummy")
        assert len(client.calls) == 3


def test_archive_timeout_marks_unknown_no_automatic_duplicate():
    factory, pub_id = _success_setup()
    client = FakeClient(fail=True)
    with factory() as db:
        store = SimpleNamespace(path_for=lambda _key: Path("/tmp/p2.mp4"))
        record = _upload_then_append(db, pub_id, client=client, store=store, csrf="dummy")
        assert record["state"] == "unknown"
        _upload_then_append(db, pub_id, client=client, store=store, csrf="dummy")
        assert sum(call[0] == "edit" for call in client.calls) == 1


def test_creator_edit_uses_original_metadata_and_p1():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"code": 0, "data": {"aid": 98765}}
    bili = MagicMock()
    bili.post.return_value = response
    client = object.__new__(BilibiliWebClient)
    client._bili = bili
    original = {
        "archive": {"aid": 98765, "title": "原视频", "copyright": 2,
                    "source": "https://youtube.com/example", "tid": 201,
                    "tag": "科普,电路", "desc": "原简介", "cover": "//example.com/pic.jpg"},
        "videos": [{"filename": "p1", "title": "原视频", "desc": "", "cid": 101}],
    }
    client.append_archive_part(
        archive=original, aid=98765,
        uploaded=UploadedVideo("p2", 202, "upload-id", "upos://p2"),
        title="【中配】火箭引擎", csrf="fake")
    args = bili.post.call_args
    body = args.kwargs["json"]
    assert "x/vu/web/edit" in args.args[0]
    assert body["aid"] == 98765 and len(body["videos"]) == 2
    assert body["videos"][0]["cid"] == 101
    assert body["videos"][1]["cid"] == 202
    assert body["title"] == "原视频"
    assert body["desc"] == "原简介"
    assert body["tag"] == "科普,电路"
    with pytest.raises(BilibiliWebError):
        client.append_archive_part(
            archive={"archive": original["archive"], "videos": original["videos"] * 2},
            aid=98765, uploaded=UploadedVideo("p2", 202, "id", "uri"),
            title="【中配】火箭引擎", csrf="fake")
