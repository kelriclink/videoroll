from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

import pytest
from fastapi import HTTPException

from videoroll.apps.orchestrator_api.services import youtube_service
from videoroll.db.models import AppSetting, SourceLicense, Task


class _ProgressDb:
    def __init__(self, task_id: uuid.UUID | None = None) -> None:
        self.tasks = {task_id: object()} if task_id else {}
        self.settings: dict[str, AppSetting] = {}

    def get(self, model: object, key: object) -> object | None:
        if model is Task:
            return self.tasks.get(key)  # type: ignore[arg-type]
        if model is AppSetting:
            return self.settings.get(str(key))
        raise AssertionError(f"unexpected model: {model}")

    def add(self, row: object) -> None:
        if isinstance(row, AppSetting):
            self.settings[row.key] = row

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


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


def test_proxy_test_rejects_non_youtube_target_before_network() -> None:
    settings = SimpleNamespace(youtube_user_agent="UA/1.0")
    with patch.object(youtube_service.httpx, "Client") as client:
        result = youtube_service.test_proxy(
            url="http://127.0.0.1:8000/private",
            proxy="",
            settings=settings,  # type: ignore[arg-type]
        )

    assert result.ok is False
    assert "YouTube URL" in str(result.error)
    client.assert_not_called()


def test_download_progress_returns_idle_for_task_without_saved_progress() -> None:
    task_id = uuid.uuid4()
    with patch(
        "videoroll.apps.youtube_ingest.download_dispatch.download_operation_status",
        return_value=None,
    ):
        progress = youtube_service.get_download_progress(
            task_id,
            db=_ProgressDb(task_id),  # type: ignore[arg-type]
        )

    assert progress == {
        "task_id": str(task_id),
        "status": "idle",
        "active": False,
        "progress": 0,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed_bytes_per_second": None,
        "eta_seconds": None,
        "filename": None,
        "error": None,
        "updated_at": None,
    }


def test_duplicate_download_caller_does_not_mark_shared_progress_failed() -> None:
    task_id = uuid.uuid4()
    db = Mock()
    reporter = Mock()
    claim = SimpleNamespace(acquired=False, result_json=None)
    settings = SimpleNamespace(redis_url="", database_url="sqlite:///:memory:")

    with (
        patch.object(youtube_service, "_YouTubeDownloadProgressReporter", return_value=reporter),
        patch.object(youtube_service, "claim_operation", return_value=claim),
        patch.object(youtube_service, "release_operation") as release_operation,
        pytest.raises(youtube_service.YouTubeConflict) as caught,
    ):
        youtube_service.download(task_id, settings=settings, db=db, store=Mock())  # type: ignore[arg-type]

    assert caught.value.code == "YOUTUBE_DOWNLOAD_IN_PROGRESS"
    reporter.fail.assert_not_called()
    release_operation.assert_not_called()


def test_download_progress_returns_404_for_missing_task() -> None:
    with pytest.raises(HTTPException) as exc_info:
        youtube_service.get_download_progress(uuid.uuid4(), db=_ProgressDb())  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404


def test_download_progress_reporter_persists_and_publishes_updates() -> None:
    task_id = uuid.uuid4()
    db = _ProgressDb(task_id)
    redis_client = Mock()
    with patch.object(youtube_service.Redis, "from_url", return_value=redis_client):
        reporter = youtube_service._YouTubeDownloadProgressReporter(
            task_id,
            db=db,  # type: ignore[arg-type]
            redis_url="redis://localhost:6379/0",
        )

    with (
        patch.object(youtube_service.time, "monotonic", side_effect=[0.0, 0.6, 1.2]),
        patch.object(youtube_service, "publish_ui_event") as publish_event,
    ):
        reporter.update("preparing", 0)
        reporter.hook(
            {
                "status": "downloading",
                "downloaded_bytes": 25,
                "total_bytes": 100,
                "speed": 10,
                "eta": 8,
                "filename": "/tmp/demo.mp4.part",
                "info_dict": {"format_id": "video"},
            }
        )
        reporter.update("completed", 100, speed_bytes_per_second=None, eta_seconds=0)

    saved = db.settings[f"{youtube_service.YOUTUBE_DOWNLOAD_PROGRESS_PREFIX}{task_id}"].value_json
    assert saved["status"] == "completed"
    assert saved["active"] is False
    assert saved["progress"] == 100
    assert saved["downloaded_bytes"] == 25
    assert saved["total_bytes"] == 100
    assert saved["filename"] == "demo.mp4.part"
    assert saved["updated_at"]
    assert publish_event.call_args.kwargs["topics"] == [f"task:{task_id}"]
    assert publish_event.call_args.kwargs["name"] == "youtube_download.progress"
    assert redis_client.set.call_count == 3


def test_download_progress_reporter_keeps_last_progress_when_download_fails() -> None:
    task_id = uuid.uuid4()
    db = _ProgressDb(task_id)
    reporter = youtube_service._YouTubeDownloadProgressReporter(task_id, db=db, redis_url="")  # type: ignore[arg-type]
    with patch.object(youtube_service.time, "monotonic", side_effect=[0.0, 0.6, 1.2]):
        reporter.update("preparing", 0)
        reporter.hook(
            {
                "status": "downloading",
                "downloaded_bytes": 50,
                "total_bytes": 100,
                "filename": "demo.webm.part",
            }
        )
        reporter.fail(RuntimeError("connection reset"))

    saved = db.settings[f"{youtube_service.YOUTUBE_DOWNLOAD_PROGRESS_PREFIX}{task_id}"].value_json
    assert saved["status"] == "failed"
    assert saved["active"] is False
    assert saved["progress"] == 47
    assert saved["downloaded_bytes"] == 50
    assert saved["filename"] == "demo.webm.part"
    assert saved["error"] == "connection reset"


def test_download_progress_reporter_throttles_frequent_download_hooks() -> None:
    task_id = uuid.uuid4()
    db = _ProgressDb(task_id)
    redis_client = Mock()
    with patch.object(youtube_service.Redis, "from_url", return_value=redis_client):
        reporter = youtube_service._YouTubeDownloadProgressReporter(
            task_id,
            db=db,  # type: ignore[arg-type]
            redis_url="redis://localhost:6379/0",
        )

    with (
        patch.object(youtube_service.time, "monotonic", side_effect=[0.0, 0.1, 0.6]),
        patch.object(youtube_service, "publish_ui_event") as publish_event,
    ):
        reporter.update("preparing", 0)
        reporter.hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100})
        reporter.hook({"status": "downloading", "downloaded_bytes": 20, "total_bytes": 100})

    saved = db.settings[f"{youtube_service.YOUTUBE_DOWNLOAD_PROGRESS_PREFIX}{task_id}"].value_json
    assert saved["progress"] == 19
    assert saved["downloaded_bytes"] == 20
    assert publish_event.call_count == 2


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
    store = Mock()
    settings = SimpleNamespace(work_dir=str(tmp_path))
    meta = SimpleNamespace(title="Demo", description="", webpage_url=task.source_url)

    with (
        patch.object(youtube_service, "effective_youtube_settings", return_value=settings),
        patch.object(youtube_service, "extract_youtube_metadata", return_value=({"title": "Demo"}, meta)),
        patch.object(youtube_service, "queue_pending_storage_delete") as queue_delete,
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        youtube_service.fetch_meta(task_id, settings=settings, db=db, store=store)  # type: ignore[arg-type]

    uploaded_key = store.put_bytes.call_args.args[1]
    assert uploaded_key.startswith(f"raw/{task_id}/metadata_")
    assert uploaded_key.count("_") >= 2
    queue_delete.assert_called_once_with(db, uploaded_key, reason="failed_youtube_upload")
    store.delete_object.assert_not_called()


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
    store = Mock()
    body = Mock()
    body.read.return_value = payload
    store.get_object.return_value = {"Body": body}
    settings = SimpleNamespace(work_dir=str(tmp_path))
    meta = SimpleNamespace(title="Demo", description="", webpage_url=source_url)

    with (
        patch.object(youtube_service, "effective_youtube_settings", return_value=settings),
        patch.object(youtube_service, "summarize_info", return_value=meta),
        patch.object(youtube_service, "download_thumbnail_jpg", return_value=None),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        youtube_service._download(  # type: ignore[arg-type]
            task_id,
            settings=settings,
            db=db,
            store=store,
            reporter=Mock(),
        )

    deleted_keys = [call.args[0] for call in store.delete_object.call_args_list]
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
    store = Mock()
    settings = SimpleNamespace(work_dir=str(tmp_path))
    meta = SimpleNamespace(title="Demo", description="", webpage_url=task.source_url)

    with (
        patch.object(youtube_service, "effective_youtube_settings", return_value=settings),
        patch.object(youtube_service, "extract_youtube_metadata", return_value=({"title": "Demo"}, meta)),
        patch.object(youtube_service, "queue_pending_storage_delete"),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        youtube_service.fetch_meta(task_id, settings=settings, db=db, store=store)  # type: ignore[arg-type]

    store.delete_object.assert_not_called()

def test_youtube_download_releases_settings_transaction_before_network(tmp_path) -> None:
    """Regression: do not hold 60-second PostgreSQL idle transaction during yt-dlp."""
    from videoroll.db.models import SourceType, TaskStatus
    task_id = uuid.uuid4()
    task = SimpleNamespace(source_url="https://www.youtube.com/watch?v=HZMiJ_Q47qk",
                           source_type=SourceType.youtube,status=TaskStatus.ingested)
    timeline = []
    db = Mock()
    db.get.return_value = task
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    db.commit.side_effect = lambda: timeline.append("commit")
    reporter = Mock()
    store = Mock()
    settings = SimpleNamespace(work_dir=str(tmp_path))
    def fail_before_file(*args, **kwargs):
        timeline.append("download")
        raise RuntimeError("upstream connection reset")
    with patch.object(youtube_service, "effective_youtube_settings", return_value=object()), \
         patch.object(youtube_service, "download_youtube_video", side_effect=fail_before_file), \
         patch.object(youtube_service, "youtube_bot_check_hint", return_value=""), \
         patch.object(youtube_service, "_store_failure_log"):
        with pytest.raises(HTTPException, match="upstream connection reset"):
            youtube_service._download(task_id, settings=settings, db=db, store=store, reporter=reporter)
    assert timeline.index("commit") < timeline.index("download")
    db.rollback.assert_called()


def test_successful_youtube_download_cleanup_removes_partial_files(tmp_path) -> None:
    """A completed download should clean temporary aria2/yt-dlp files."""
    root = tmp_path / "youtube" / "task-id"
    media = root / "media-download" / "parallel"
    media.mkdir(parents=True)
    (media / "video.webm.part.aria2").write_text("resume", encoding="utf-8")
    youtube_service._cleanup_completed_youtube_media(root)
    assert not (root / "media-download").exists()


def test_successful_youtube_download_cleanup_error_is_nonfatal(tmp_path, caplog) -> None:
    """Post-commit workdir errors must not mark a saved video as failed."""
    root = tmp_path / "youtube" / "task-id"
    media = root / "media-download"
    media.mkdir(parents=True)
    with patch.object(youtube_service.shutil, "rmtree", side_effect=PermissionError("busy workdir")):
        youtube_service._cleanup_completed_youtube_media(root, task_id=uuid.uuid4())
    assert media.exists()
    assert "temporary media cleanup failed" in caplog.text


def test_existing_video_download_retry_succeeds_even_if_workdir_cleanup_fails(tmp_path) -> None:
    """Regression for a real 3+ GB video saved before post-download NameError."""
    from videoroll.db.models import Asset, AssetKind, SourceType, TaskStatus

    task_id = uuid.uuid4()
    source_url = "https://www.youtube.com/watch?v=63lqU99buno"
    task = SimpleNamespace(
        id=task_id, source_url=source_url,
        source_type=SourceType.youtube, status=TaskStatus.downloaded,
    )
    video_key = f"raw/{task_id}/video_saved.webm"
    video_asset = Asset(
        id=uuid.uuid4(), task_id=task_id, kind=AssetKind.video_raw,
        storage_key=video_key, sha256="0" * 64, size_bytes=3_300_000_000,
        duration_ms=None, created_at=datetime.now(timezone.utc),
    )
    metadata_asset = Asset(
        id=uuid.uuid4(), task_id=task_id, kind=AssetKind.metadata_json,
        storage_key=f"raw/{task_id}/metadata.json", sha256="1" * 64,
        size_bytes=100, duration_ms=None, created_at=datetime.now(timezone.utc),
    )
    info = {"title": "Existing YouTube download", "id": "63lqU99buno"}
    db = Mock()
    db.get.return_value = task
    def apply_db_defaults(row):
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        if getattr(row, "created_at", None) is None:
            row.created_at = datetime.now(timezone.utc)
    db.refresh.side_effect = apply_db_defaults
    query = Mock()
    db.query.return_value = query
    query.filter.return_value.order_by.return_value.first.side_effect = [
        video_asset, metadata_asset,
    ]
    store = Mock()
    store.get_object.return_value = {
        "Body": SimpleNamespace(read=lambda: youtube_service.json.dumps(info).encode()),
    }
    settings = SimpleNamespace(work_dir=str(tmp_path))
    meta = SimpleNamespace(title="Existing YouTube download", description="", webpage_url=source_url)
    reporter = Mock()

    real_rmtree = youtube_service.shutil.rmtree

    def fail_just_media_cleanup(path, *args, **kwargs):
        if str(path).endswith("media-download"):
            raise PermissionError("busy workdir")
        return real_rmtree(path, *args, **kwargs)

    with (
        patch.object(youtube_service, "effective_youtube_settings", return_value=object()),
        patch.object(youtube_service, "summarize_info", return_value=meta),
        patch.object(youtube_service, "download_thumbnail_jpg", return_value=None),
        patch.object(youtube_service, "snapshot_downloaded_youtube_info"),
        patch.object(youtube_service, "download_youtube_video") as download_again,
        patch.object(youtube_service.shutil, "rmtree", side_effect=fail_just_media_cleanup),
    ):
        response = youtube_service._download(
            task_id, settings=settings, db=db, store=store, reporter=reporter
        )
    assert response.video_asset.storage_key == video_key
    download_again.assert_not_called()
    store.head_object.assert_called_once_with(video_key)
    reporter.update.assert_any_call("completed", 100, speed_bytes_per_second=None, eta_seconds=0)
