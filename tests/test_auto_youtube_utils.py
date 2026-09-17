from __future__ import annotations

import unittest

from videoroll.utils.auto_youtube import encode_auto_youtube_created_by, parse_auto_youtube_created_by


class AutoYouTubeUtilsTests(unittest.TestCase):
    def test_encode_and_parse_with_auto_publish(self) -> None:
        raw = encode_auto_youtube_created_by("youtube_home_scan", auto_publish=True)
        parsed = parse_auto_youtube_created_by(raw)

        self.assertEqual(parsed, {"origin": "youtube_home_scan", "auto_publish": True})

    def test_encode_and_parse_run_id(self) -> None:
        raw = encode_auto_youtube_created_by(
            "auto_youtube",
            auto_publish=None,
            run_id="run_123-abc",
        )
        self.assertEqual(
            parse_auto_youtube_created_by(raw),
            {"origin": "auto_youtube", "auto_publish": None, "run_id": "run_123-abc"},
        )

    def test_parse_rejects_non_auto_marker(self) -> None:
        self.assertIsNone(parse_auto_youtube_created_by("web"))


if __name__ == "__main__":
    unittest.main()
