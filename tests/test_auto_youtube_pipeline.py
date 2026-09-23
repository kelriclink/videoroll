from __future__ import annotations

import unittest
import uuid
from unittest.mock import MagicMock, patch

from videoroll.apps.subtitle_service.worker import after_render_publish
from videoroll.utils.auto_youtube import parse_auto_youtube_created_by


class AutoYouTubePipelineTests(unittest.TestCase):

    def test_after_render_publish_uses_orchestrator_publisher_configuration(self) -> None:
        """Automatic publishing must receive the publisher endpoint settings."""
        task_id = uuid.uuid4()
        render_job_id = uuid.uuid4()
        render_job = MagicMock(task_id=task_id)
        render_job.request_json = {
            "after_render": {
                "publish": True,
                "publish_payload": {"skip_review": True},
            }
        }
        task = MagicMock(id=task_id)
        db = MagicMock()
        db.get.side_effect = lambda model, _id: render_job if _id == render_job_id else task

        with (
            patch("videoroll.apps.subtitle_service.worker._ensure_db"),
            patch("videoroll.apps.subtitle_service.worker._db", return_value=db),
            patch("videoroll.apps.subtitle_service.worker.FileStore"),
            patch(
                "videoroll.apps.orchestrator_api.services.publishing_service.publish_all",
                return_value={"has_any_accepted": True, "errors": {}},
            ) as publish_all,
        ):
            result = after_render_publish.run(str(render_job_id))

        self.assertEqual(result["status"], "ok")
        publisher_settings = publish_all.call_args.args[2]
        self.assertTrue(hasattr(publisher_settings, "bilibili_publisher_url"))
        self.assertTrue(hasattr(publisher_settings, "social_publisher_url"))

    def test_auto_youtube_intake_does_not_freeze_auto_publish(self) -> None:
        from videoroll.apps.orchestrator_api.services import youtube_service
        from videoroll.db.models import SourceLicense

        task_id = uuid.uuid4()
        settings = object()
        with (
            patch.object(youtube_service, "ingest_youtube_source", return_value=(task_id, False, "source-1")),
            patch.object(youtube_service, "enqueue_auto_youtube_pipeline", return_value="pipeline-1") as enqueue,
            patch.object(youtube_service, "set_task_created_by") as set_created_by,
        ):
            result = youtube_service.start_auto_youtube_pipeline(
                url="https://www.youtube.com/watch?v=demo",
                license=SourceLicense.authorized,
                proof_url="https://example.com/proof",
                auto_publish=True,
                settings=settings,  # type: ignore[arg-type]
            )

        self.assertEqual(result.pipeline_job_id, "pipeline-1")
        created_by = set_created_by.call_args.kwargs["created_by"]
        parsed = parse_auto_youtube_created_by(created_by)
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed["auto_publish"])
        enqueue.assert_called_once_with(task_id, auto_publish=None, settings=settings)

    def test_enqueue_auto_youtube_pipeline_never_freezes_auto_publish_snapshot(self) -> None:
        from videoroll.apps.orchestrator_api.services import youtube_service

        task_id = uuid.uuid4()
        settings = object()
        launcher = MagicMock()
        launcher.launch_auto_youtube.return_value.external_run_id = "hatchet-1"
        with patch.object(youtube_service, "PipelineLauncher", return_value=launcher) as launcher_cls:
            result = youtube_service.enqueue_auto_youtube_pipeline(
                task_id,
                auto_publish=True,
                settings=settings,  # type: ignore[arg-type]
            )

        self.assertEqual(result, "hatchet-1")
        launcher_cls.assert_called_once_with(settings)
        launcher.launch_auto_youtube.assert_called_once_with(task_id)

    def test_deduped_auto_youtube_does_not_enqueue_a_second_pipeline(self) -> None:
        from videoroll.apps.orchestrator_api.services import youtube_service
        from videoroll.db.models import SourceLicense

        task_id = uuid.uuid4()
        with (
            patch.object(
                youtube_service,
                "ingest_youtube_source",
                return_value=(task_id, True, "source-1"),
            ),
            patch.object(youtube_service, "enqueue_auto_youtube_pipeline") as enqueue,
            patch.object(youtube_service, "set_task_created_by") as set_created_by,
        ):
            result = youtube_service.start_auto_youtube_pipeline(
                url="https://www.youtube.com/watch?v=demo",
                license=SourceLicense.authorized,
                proof_url="https://example.com/proof",
                auto_publish=True,
                settings=object(),  # type: ignore[arg-type]
            )

        self.assertEqual(result.task_id, task_id)
        self.assertTrue(result.deduped)
        self.assertIsNone(result.pipeline_job_id)
        enqueue.assert_not_called()
        set_created_by.assert_not_called()


if __name__ == "__main__":
    unittest.main()
