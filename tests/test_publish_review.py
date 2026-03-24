from __future__ import annotations

import unittest
from unittest.mock import patch

from videoroll.apps.publish_review import (
    extract_subtitle_plain_text,
    normalize_blocked_words,
    review_publish_materials,
)


class PublishReviewTests(unittest.TestCase):
    def test_normalize_blocked_words_dedupes_and_trims(self) -> None:
        self.assertEqual(normalize_blocked_words([" 诈骗 ", "诈骗", "", "成人内容"]), ["诈骗", "成人内容"])

    def test_extract_subtitle_plain_text_strips_metadata_and_tags(self) -> None:
        raw = """1
00:00:01,000 --> 00:00:02,000
Hello <i>world</i>

2
00:00:03,000 --> 00:00:04,000
{\\an8}你好\\N世界
"""
        self.assertEqual(extract_subtitle_plain_text(raw), "Hello world 你好 世界")

    def test_review_publish_materials_rejects_blocked_words_without_ai(self) -> None:
        result = review_publish_materials(
            title="这是一个诈骗教程",
            summary="",
            subtitle_text="",
            blocked_words=["诈骗", "赌博"],
            reject_rules="",
            config=None,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["review_mode"], "blocked_words")
        self.assertEqual(result["matched_blocked_words"], ["诈骗"])

    def test_review_publish_materials_requires_openai_when_enabled(self) -> None:
        result = review_publish_materials(
            title="普通评测视频",
            summary="内容正常",
            subtitle_text="这是字幕",
            blocked_words=[],
            reject_rules="不允许危险教程",
            config=None,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["review_mode"], "config_missing")

    def test_review_publish_materials_uses_ai_result(self) -> None:
        with patch("videoroll.apps.publish_review.review_publish_content_openai") as mocked:
            mocked.return_value = {
                "approved": False,
                "reason": "包含危险行为教学",
                "risk_tags": ["危险教程", "高风险"],
            }
            result = review_publish_materials(
                title="某实验演示",
                summary="总结",
                subtitle_text="字幕内容",
                blocked_words=[],
                reject_rules="不允许危险教程",
                config=object(),  # type: ignore[arg-type]
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["review_mode"], "ai")
        self.assertEqual(result["reason"], "包含危险行为教学")
        self.assertEqual(result["risk_tags"], ["危险教程", "高风险"])


if __name__ == "__main__":
    unittest.main()
