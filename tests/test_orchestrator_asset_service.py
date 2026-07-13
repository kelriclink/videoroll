from __future__ import annotations

import asyncio
import io
from pathlib import Path
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

from videoroll.apps.orchestrator_api.services import asset_service
from videoroll.db.models import AppSetting, Asset, AssetKind


def test_inline_cover_asset_has_nosniff_and_is_forced_to_attachment() -> None:
    task_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    asset = Mock(
        id=asset_id,
        task_id=task_id,
        kind=AssetKind.cover_image,
        storage_key="final/cover.svg",
        size_bytes=24,
    )
    db = Mock()
    db.get.side_effect = lambda model, key: asset if model is Asset and key == asset_id else None
    s3 = Mock()
    s3.head_object.return_value = {
        "ContentLength": 24,
        "ContentType": "image/svg+xml",
    }
    s3.get_object.return_value = {
        "Body": io.BytesIO(b"<svg/onload=alert(1)>"),
        "ContentLength": 24,
        "ContentType": "image/svg+xml",
    }

    result = asset_service.prepare_asset_stream(
        db,
        s3,
        task_id=task_id,
        asset_id=asset_id,
        range_header="",
    )

    assert result.media_type == "application/octet-stream"
    assert result.headers["X-Content-Type-Options"] == "nosniff"
    assert result.headers["Content-Disposition"].startswith("attachment")


def test_video_asset_with_safe_type_remains_inline() -> None:
    asset = Mock(kind=AssetKind.video_final, storage_key="final/video.mp4")

    headers = asset_service.safe_asset_headers(asset, "video/mp4", inline=True)

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Disposition"].startswith("inline")


def test_video_asset_with_active_content_type_is_not_inline() -> None:
    asset = Mock(kind=AssetKind.video_final, storage_key="final/video.mp4")

    headers = asset_service.safe_asset_headers(asset, "text/html", inline=True)

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Disposition"].startswith("attachment")


def test_delete_final_asset_defers_object_delete_until_a_later_retry() -> None:
    events: list[str] = []
    task_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    asset = Mock(id=asset_id, task_id=task_id, kind=AssetKind.video_final, storage_key="final/video.mp4")
    db = Mock()
    db.get.side_effect = lambda model, key: asset if model is Asset and key == asset_id else None
    db.add.side_effect = lambda row: events.append("queue") if isinstance(row, AppSetting) else None
    db.commit.side_effect = lambda: events.append("commit")
    db.query.return_value.filter.return_value.delete.return_value = 0
    s3 = Mock()

    result = asset_service.delete_final_asset(task_id=task_id, asset_id=asset_id, db=db, s3=s3)

    assert result == {"deleted": True}
    assert events == ["queue", "commit"]
    s3.delete_object.assert_not_called()


def test_delete_final_asset_persists_cleanup_with_metadata_removal() -> None:
    task_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    asset = Mock(id=asset_id, task_id=task_id, kind=AssetKind.video_final, storage_key="final/video.mp4")
    db = Mock()
    db.get.side_effect = lambda model, key: asset if model is Asset and key == asset_id else None
    db.query.return_value.filter.return_value.delete.return_value = 0
    events: list[str] = []
    db.add.side_effect = lambda row: events.append("queue") if isinstance(row, AppSetting) else None
    db.commit.side_effect = lambda: events.append("commit")
    s3 = Mock()

    result = asset_service.delete_final_asset(task_id=task_id, asset_id=asset_id, db=db, s3=s3)

    assert result == {"deleted": True}
    pending_rows = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], AppSetting)]
    assert len(pending_rows) == 1
    assert pending_rows[0].value_json["storage_key"] == "final/video.mp4"
    assert events == ["queue", "commit"]
    s3.delete_object.assert_not_called()


def test_failed_deterministic_upload_is_queued_instead_of_deleted(tmp_path: Path) -> None:
    task = Mock(id=uuid.uuid4(), status=None)
    upload = Mock(filename="video.mp4", content_type="video/mp4")
    upload.seek = AsyncMock()
    upload.close = AsyncMock()
    db = Mock()
    db.commit.side_effect = RuntimeError("database unavailable")
    s3 = Mock()
    temp_path = tmp_path / "video.mp4"
    temp_path.write_bytes(b"video")

    async def fake_threadpool(function, *args, **kwargs):
        if function is asset_service.stream_upload_to_tempfile:
            return temp_path, "a" * 64, 5
        return function(*args, **kwargs)

    with (
        patch.object(asset_service, "run_in_threadpool", side_effect=fake_threadpool),
        patch.object(asset_service, "queue_pending_s3_delete") as queue_delete,
        pytest.raises(Exception, match="upload failed"),
    ):
        asyncio.run(
            asset_service.store_uploaded_task_asset(
                task=task,
                file=upload,
                s3=s3,
                db=db,
                temp_prefix="upload_",
                default_suffix=".mp4",
                key_prefix="raw",
                object_name_prefix="video",
                asset_kind=AssetKind.video_raw,
            )
        )

    storage_key = queue_delete.call_args.args[1]
    assert storage_key.startswith(f"raw/{task.id}/video_{'a' * 16}_")
    assert storage_key.endswith(".mp4")
    assert storage_key != f"raw/{task.id}/video_{'a' * 16}.mp4"
    queue_delete.assert_called_once_with(db, storage_key, reason="failed_asset_upload")
    s3.delete_object.assert_not_called()


def test_pending_delete_is_canceled_when_storage_key_is_referenced_again() -> None:
    storage_key = "final/video.mp4"
    row = AppSetting(
        key=asset_service.pending_s3_delete_key(storage_key),
        value_json={"storage_key": storage_key},
    )
    pending_query = Mock()
    pending_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [row]
    reference_query = Mock()
    reference_query.filter.return_value.first.return_value = object()
    db = Mock()
    db.query.side_effect = lambda model: pending_query if model is AppSetting else reference_query
    s3 = Mock()

    deleted = asset_service.retry_pending_s3_deletes(db, s3)

    assert deleted == 0
    s3.delete_object.assert_not_called()
    db.delete.assert_called_once_with(row)
