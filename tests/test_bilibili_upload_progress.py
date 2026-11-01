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


def test_upload_progress_does_not_leave_long_lived_database_transaction() -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from videoroll.apps.bilibili_publisher.worker import _set_bilibili_upload_progress
    from videoroll.db.base import Base
    from videoroll.db.models import PublishJob, PublishState, SourceLicense, SourceType, Task

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Task.__table__, PublishJob.__table__])
    try:
        with Session(engine) as db:
            task = Task(source_type=SourceType.youtube, source_license=SourceLicense.authorized)
            db.add(task)
            db.flush()
            job = PublishJob(task_id=task.id, state=PublishState.submitting, upload_active=True, upload_progress=0)
            db.add(job)
            db.commit()

            # This ORM SELECT starts a transaction. A multipart upload may run
            # for several minutes without a percentage change, so a no-op
            # progress update must still release that transaction.
            db.scalars(select(PublishJob)).one()
            assert db.in_transaction()
            _set_bilibili_upload_progress(db, job, active=True, progress=0)
            assert not db.in_transaction()

            # Persist a real update while making sure the next network wait
            # starts without an open transaction too.
            _set_bilibili_upload_progress(db, job, active=True, progress=17)
            assert not db.in_transaction()
            db.expire_all()
            assert db.get(PublishJob, job.id).upload_progress == 17
    finally:
        engine.dispose()
