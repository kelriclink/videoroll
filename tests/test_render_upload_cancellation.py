from __future__ import annotations

import asyncio
import io
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile

from videoroll.apps.orchestrator_api.services import asset_service
from videoroll.db.models import AssetKind


def test_canceled_upload_does_not_register_a_final_asset() -> None:
    db = MagicMock()
    store = MagicMock()
    task = MagicMock(id=uuid.uuid4())
    upload = UploadFile(file=io.BytesIO(b"streamed video"), filename="render.mp4")
    examined = []

    def canceled_before_commit(session):
        assert session is db
        store.upload_file.assert_called_once()
        examined.append(True)
        raise HTTPException(409, "render canceled during upload")

    with patch.object(asset_service, "queue_pending_storage_delete") as cleanup:
        with pytest.raises(HTTPException) as error:
            asyncio.run(asset_service.store_uploaded_task_asset(
                task=task, file=upload, store=store, db=db,
                temp_prefix="test_render_", default_suffix=".mp4", key_prefix="final",
                object_name_prefix="video_remote", asset_kind=AssetKind.video_final,
                max_bytes=1024, before_commit=canceled_before_commit,
            ))

    assert error.value.status_code == 409
    assert examined == [True]
    db.add.assert_not_called()
    db.commit.assert_not_called()
    cleanup.assert_called_once()
    assert cleanup.call_args.kwargs["reason"] == "rejected_asset_upload"
