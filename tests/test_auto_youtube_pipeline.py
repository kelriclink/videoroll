from __future__ import annotations

import json
import unittest
import uuid
from unittest.mock import MagicMock, patch

from videoroll.apps.subtitle_service.worker import (
    _build_after_render_publish_action,
    after_render_publish,
)


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_bytes(self, data: bytes, key: str, *, content_type: str) -> None:
        self.calls.append(
            {
                "data": data,
                "key": key,
                "content_type": content_type,
            }
        )


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
            patch("videoroll.apps.subtitle_service.worker.S3Store"),
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

    def test_build_after_render_publish_action_returns_none_when_disabled(self) -> None:
        action = _build_after_render_publish_action(
            task_id=uuid.uuid4(),
            cover_key=None,
            profile={"auto_publish": False},
            yt_title="",
            yt_desc="",
            webpage_url="",
            db=object(),  # type: ignore[arg-type]
            store=_FakeStore(),  # type: ignore[arg-type]
        )

        self.assertIsNone(action)

    def test_build_after_render_publish_action_persists_meta_and_returns_payload(self) -> None:
        task_id = uuid.uuid4()
        store = _FakeStore()
        profile = {
            "auto_publish": True,
            "publish_typeid_mode": "ai_summary",
            "publish_title_prefix": "【熟肉】",
        }
        final_meta = {"title": "Final Title", "desc": "Final Desc"}

        with (
            patch("videoroll.apps.subtitle_service.worker.default_publish_meta", return_value={"title": ""}),
            patch("videoroll.apps.subtitle_service.worker.get_translate_settings", return_value={"provider": "openai"}),
            patch("videoroll.apps.subtitle_service.worker.apply_publish_source_overrides", return_value=final_meta),
        ):
            action = _build_after_render_publish_action(
                task_id=task_id,
                cover_key="cover/key.jpg",
                profile=profile,
                yt_title="Source Title",
                yt_desc="Source Description",
                webpage_url="https://www.youtube.com/watch?v=demo",
                db=object(),  # type: ignore[arg-type]
                store=store,  # type: ignore[arg-type]
            )

        self.assertEqual(
            action,
            {
                "publish": True,
                "publish_payload": {
                    "account_id": None,
                    "video_key": None,
                    "cover_key": "cover/key.jpg",
                    "typeid_mode": "ai_summary",
                    "meta": None,
                },
            },
        )
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0]["key"], f"meta/{task_id}/publish_meta.json")
        self.assertEqual(store.calls[0]["content_type"], "application/json")
        self.assertEqual(json.loads(store.calls[0]["data"].decode("utf-8")), final_meta)

    def test_remote_auto_youtube_accepts_bearer_token_before_query_token(self) -> None:
        from starlette.requests import Request

        from videoroll.apps.orchestrator_api.routers import youtube as youtube_router
        from videoroll.db.models import SourceLicense

        task_id = uuid.uuid4()
        seen_tokens: list[str] = []
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/remote/auto/youtube",
                "headers": [(b"authorization", b"Bearer header-token")],
                "query_string": b"",
            }
        )

        def verify_token(_db: object, token: str) -> bool:
            seen_tokens.append(token)
            return token == "header-token"

        with (
            patch("videoroll.apps.orchestrator_api.routers.youtube.remote_api_token_is_configured", return_value=True),
            patch("videoroll.apps.orchestrator_api.routers.youtube.verify_remote_api_token", side_effect=verify_token),
            patch(
                "videoroll.apps.orchestrator_api.routers.youtube.youtube_service.start_auto_youtube_pipeline",
                return_value=youtube_router.AutoYouTubeResponse(task_id=task_id, pipeline_job_id="job-1"),
            ) as start_pipeline,
        ):
            result = youtube_router.remote_auto_youtube(
                request,
                url="https://www.youtube.com/watch?v=demo",
                token="query-token",
                license=SourceLicense.authorized,
                proof_url="https://example.com/proof",
                auto_publish=True,
                settings=object(),  # type: ignore[arg-type]
                db=object(),  # type: ignore[arg-type]
            )

        self.assertEqual(seen_tokens, ["header-token"])
        self.assertEqual(result.task_id, task_id)
        start_pipeline.assert_called_once()
        self.assertEqual(start_pipeline.call_args.kwargs["auto_publish"], True)


if __name__ == "__main__":
    unittest.main()
