"""Automatic YouDub is a visible prerequisite to one-shot P1+P2 publish."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from videoroll.apps.orchestrator_api.services.publishing_service import auto_publish_task
from videoroll.apps.orchestrator_api.routers.youdub_native_routes import auto_dubbing
from videoroll.db.models import Asset, AssetKind, DubbingJob, Task


def test_publishing_p1_p2_as_one_archive_submission() -> None:
    task_id, dub_id = uuid.uuid4(), uuid.uuid4()
    task = MagicMock(id=task_id)
    finished = MagicMock(
        id=dub_id, task_id=task_id, status="succeeded",
        request_json={
            "auto_p2_source": "video_pipeline",
            "auto_p1_prefix": "【熟肉】",
            "auto_p2_prefix": "【中配】",
        },
        output_json={"video_key": "dubbing/p2.mp4"},
    )
    db = MagicMock()
    db.get.side_effect = lambda model, _id: (
        task if model is Task else finished if model is DubbingJob else None
    )
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        MagicMock(storage_key="render/p1.mp4")
    )
    db.query.return_value.filter.return_value.first.return_value = (
        MagicMock(storage_key="dubbing/p2.mp4", kind=AssetKind.video_dubbed)
    )
    result = {
        "has_any_accepted": True, "batch_id": "batch-one",
        "errors": {}, "results": {"bilibili": {"status": "accepted"}},
    }

    with (
        patch("videoroll.apps.orchestrator_api.services.publishing_service.get_auto_profile",
              return_value={"auto_publish": True, "auto_publish_platforms": ["bilibili"]}),
        patch("videoroll.apps.orchestrator_api.services.publishing_service.build_auto_publish_after_render",
              return_value={"publish_payload": {
                  "platforms": ["bilibili"], "video_key": None, "skip_review": True,
              }}),
        patch("videoroll.apps.orchestrator_api.services.publishing_service.read_storage_json_object",
              return_value={"title": "【熟肉】火箭引擎原理", "desc": "原简介"}),
        patch("videoroll.apps.orchestrator_api.services.publishing_service.publish_all",
              return_value=result) as send,
    ):
        response = auto_publish_task(task_id, MagicMock(), db, MagicMock(), dubbing_job_id=dub_id)
    payload = send.call_args.args[1]
    assert response["publish_batch_id"] == "batch-one"
    assert payload.video_key == "render/p1.mp4"
    assert payload.platform_meta["bilibili"]["title"] == "【熟肉&中配】火箭引擎原理"
    assert payload.platform_meta["bilibili"]["desc"] == "原简介"
    assert len(payload.video_parts) == 2
    assert payload.video_parts[0]["title"] == "【熟肉】火箭引擎原理"
    assert payload.video_parts[0]["video"]["key"] == "render/p1.mp4"
    assert payload.video_parts[1]["title"] == "【中配】火箭引擎原理"
    assert payload.video_parts[1]["video"]["key"] == "dubbing/p2.mp4"
    assert payload.fresh_batch


def test_combined_archive_prefix_is_idempotent_and_respects_title_limit() -> None:
    from videoroll.apps.bilibili_publisher.auto_dub_p2 import dual_part_archive_title

    assert dual_part_archive_title("【熟肉】火箭引擎", "【熟肉】") == "【熟肉&中配】火箭引擎"
    assert dual_part_archive_title("【熟肉&中配】火箭引擎", "【熟肉】") == "【熟肉&中配】火箭引擎"
    assert dual_part_archive_title("【其他前缀】火箭引擎", "【其他前缀】") == "【熟肉&中配】火箭引擎"
    assert dual_part_archive_title("【熟肉】" + "火" * 100, "【熟肉】").startswith("【熟肉&中配】")
    assert len(dual_part_archive_title("【熟肉】" + "火" * 100, "【熟肉】")) <= 80


def test_publishing_refuses_to_send_any_part_before_dubbing_is_complete() -> None:
    task_id, dub_id = uuid.uuid4(), uuid.uuid4()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: (
        MagicMock(id=task_id) if model is Task else
        MagicMock(id=dub_id, task_id=task_id, status="synthesizing",
                  request_json={"auto_p2_source": "video_pipeline"},
                  output_json={}) if model is DubbingJob else None
    )
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        MagicMock(storage_key="render/p1.mp4")
    )
    with (
        patch("videoroll.apps.orchestrator_api.services.publishing_service.get_auto_profile",
              return_value={"auto_publish": True, "auto_publish_platforms": ["bilibili"]}),
        patch("videoroll.apps.orchestrator_api.services.publishing_service.build_auto_publish_after_render",
              return_value={"publish_payload": {"platforms": ["bilibili"], "video_key": None}}),
        patch("videoroll.apps.orchestrator_api.services.publishing_service.publish_all") as send,
    ):
        with pytest.raises(HTTPException, match="YouDub is synthesizing"):
            auto_publish_task(task_id, MagicMock(), db, MagicMock(), dubbing_job_id=dub_id)
    send.assert_not_called()


def test_auto_dubbing_is_skipped_when_disabled_and_creates_no_job() -> None:
    db = MagicMock()
    task = MagicMock()
    db.get.return_value = task
    with (
        patch("videoroll.apps.subtitle_service.auto_profile_store.get_auto_profile",
              return_value={"auto_publish": True, "auto_dub_p2": False,
                            "auto_publish_platforms": ["bilibili"]}),
        patch("videoroll.apps.orchestrator_api.routers.youdub_native_routes.create_dubbing_job") as create,
    ):
        result = auto_dubbing(uuid.uuid4(), settings=MagicMock(), db=db)
    assert result["status"] == "skipped"
    create.assert_not_called()

def test_auto_dubbing_stage_is_idempotent_and_uses_original_video() -> None:
    task_id = uuid.uuid4()
    dub_id = uuid.uuid4()
    task = MagicMock(id=task_id, created_by="auto_youtube;run_id=test")
    subtitle = MagicMock(id=uuid.uuid4(), status="succeeded")
    source = MagicMock(storage_key="raw/unedited-source.webm")
    native_job = MagicMock(
        id=dub_id, status="queued", request_json={},
    )
    db = MagicMock()
    db.get.return_value = task
    db.query.return_value.filter.return_value.order_by.return_value.first.side_effect = [
        subtitle, source,
    ]
    with (
        patch("videoroll.apps.subtitle_service.auto_profile_store.get_auto_profile",
              return_value={"auto_publish": True, "auto_dub_p2": True,
                            "auto_publish_platforms": ["bilibili"],
                            "auto_dub_p2_prefix": "【配音】", "publish_title_prefix": "【熟肉】"}),
        patch("videoroll.utils.auto_youtube.parse_auto_youtube_created_by",
              return_value={"run_id": "test"}),
        patch("videoroll.apps.orchestrator_api.routers.youdub_native_routes.ensure_ready"),
        patch("videoroll.apps.orchestrator_api.routers.youdub_native_routes.create_dubbing_job",
              return_value=native_job) as create,
        patch("videoroll.apps.orchestrator_api.routers.youdub_native_routes.PipelineLauncher") as launcher,
    ):
        result = auto_dubbing(task_id, settings=MagicMock(), db=db)

    assert result["dubbing_job_id"] == str(dub_id)
    assert result["status"] == "queued"
    assert create.call_args.kwargs["subtitle_video_key"] == "raw/unedited-source.webm"
    assert create.call_args.kwargs["operation_key"] == f"auto-p2:videopipeline:{task_id}"
    assert native_job.request_json["auto_p2_prefix"] == "【配音】"
    assert native_job.request_json["auto_p2_source"] == "video_pipeline"
    launcher.return_value.launch_dubbing_job.assert_called_once_with(
        dub_id, dispatch_key=f"video-pipeline-auto-p2:{dub_id}"
    )
