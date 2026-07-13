from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

import pytest

from videoroll.apps.orchestrator_api.services import youtube_service
from videoroll.db.models import SourceLicense


def test_ingest_uses_dedicated_internal_secret_not_s3_secret() -> None:
    response = Mock()
    response.json.return_value = {"task_id": str(uuid.uuid4()), "deduped": False, "source_id": "video-1"}
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.post.return_value = response
    settings = SimpleNamespace(
        internal_api_secret="internal-secret",
        s3_secret_access_key="unrelated-s3-secret",
        youtube_ingest_url="http://youtube-ingest",
        development_mode=False,
    )

    with patch.object(youtube_service.httpx, "Client", return_value=client) as client_factory:
        youtube_service.ingest_youtube_source(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            license=SourceLicense.authorized,
            proof_url=None,
            settings=settings,  # type: ignore[arg-type]
        )

    headers = client_factory.call_args.kwargs["headers"]
    assert headers.get("X-Videoroll-Internal-Token")


def test_home_scan_due_respects_last_finished_and_interval() -> None:
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    config = {
        "home_scan_enabled": True,
        "home_scan_interval_minutes": 60,
        "home_scan_last_finished_at": "2026-07-11T11:30:00+00:00",
    }

    assert youtube_service.home_scan_is_due(config, now=now) is False


def test_fetch_meta_queues_uploaded_object_when_db_commit_fails(tmp_path) -> None:
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, source_type=SimpleNamespace(value="youtube"), source_url="https://youtu.be/demo")
    query = Mock()
    query.filter.return_value.order_by.return_value.first.return_value = None
    query.filter.return_value.first.return_value = None
    db = Mock()
    db.get.return_value = task
    db.query.return_value = query
    db.commit.side_effect = RuntimeError("database unavailable")
    s3 = Mock()
    settings = SimpleNamespace(work_dir=str(tmp_path))
    meta = SimpleNamespace(title="Demo", description="", webpage_url=task.source_url)

    with (
        patch.object(youtube_service, "effective_youtube_settings", return_value=settings),
        patch.object(youtube_service, "extract_youtube_metadata", return_value=({"title": "Demo"}, meta)),
        patch.object(youtube_service, "queue_pending_s3_delete") as queue_delete,
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        youtube_service.fetch_meta(task_id, settings=settings, db=db, s3=s3)  # type: ignore[arg-type]

    uploaded_key = s3.put_bytes.call_args.args[1]
    assert uploaded_key.startswith(f"raw/{task_id}/metadata_")
    assert uploaded_key.count("_") >= 2
    queue_delete.assert_called_once_with(db, uploaded_key, reason="failed_youtube_upload")
    s3.delete_object.assert_not_called()


def test_download_does_not_compensate_a_preexisting_metadata_key(tmp_path) -> None:
    task_id = uuid.uuid4()
    source_url = "https://youtu.be/demo"
    task = SimpleNamespace(
        id=task_id,
        source_type=SimpleNamespace(value="youtube"),
        source_url=source_url,
        status=youtube_service.TaskStatus.downloaded,
    )
    info = {"title": "Demo"}
    payload = youtube_service.json.dumps(info, ensure_ascii=False, indent=2).encode()
    digest = youtube_service._sha256_bytes(payload)
    metadata_key = f"raw/{task_id}/metadata_{digest[:16]}.json"
    video_asset = SimpleNamespace(storage_key=f"raw/{task_id}/video.mp4")
    metadata_asset = SimpleNamespace(storage_key=metadata_key)

    db = Mock()
    db.get.return_value = task
    query = Mock()
    db.query.return_value = query
    first_results = iter([video_asset, metadata_asset, metadata_asset, None])
    query.filter.return_value.order_by.return_value.first.side_effect = lambda: next(first_results)
    db.commit.side_effect = RuntimeError("database unavailable")
    s3 = Mock()
    body = Mock()
    body.read.return_value = payload
    s3.get_object.return_value = {"Body": body}
    settings = SimpleNamespace(work_dir=str(tmp_path))
    meta = SimpleNamespace(title="Demo", description="", webpage_url=source_url)

    with (
        patch.object(youtube_service, "effective_youtube_settings", return_value=settings),
        patch.object(youtube_service, "summarize_info", return_value=meta),
        patch.object(youtube_service, "download_thumbnail_jpg", return_value=None),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        youtube_service.download(task_id, settings=settings, db=db, s3=s3)  # type: ignore[arg-type]

    deleted_keys = [call.args[0] for call in s3.delete_object.call_args_list]
    assert metadata_key not in deleted_keys


def test_fetch_meta_never_immediately_deletes_a_deterministic_key(tmp_path) -> None:
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, source_type=SimpleNamespace(value="youtube"), source_url="https://youtu.be/demo")
    db = Mock()
    db.get.return_value = task
    query = Mock()
    query.filter.return_value.order_by.return_value.first.return_value = None
    db.query.return_value = query
    db.commit.side_effect = RuntimeError("database unavailable")
    s3 = Mock()
    settings = SimpleNamespace(work_dir=str(tmp_path))
    meta = SimpleNamespace(title="Demo", description="", webpage_url=task.source_url)

    with (
        patch.object(youtube_service, "effective_youtube_settings", return_value=settings),
        patch.object(youtube_service, "extract_youtube_metadata", return_value=({"title": "Demo"}, meta)),
        patch.object(youtube_service, "queue_pending_s3_delete"),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        youtube_service.fetch_meta(task_id, settings=settings, db=db, s3=s3)  # type: ignore[arg-type]

    s3.delete_object.assert_not_called()
