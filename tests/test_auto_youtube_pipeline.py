from __future__ import annotations

import json
import unittest
import uuid
from unittest.mock import patch

from videoroll.apps.subtitle_service.worker import _build_after_render_publish_action


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


if __name__ == "__main__":
    unittest.main()
