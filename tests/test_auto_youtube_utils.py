from __future__ import annotations

import unittest

from videoroll.utils.auto_youtube import encode_auto_youtube_created_by, parse_auto_youtube_created_by


class AutoYouTubeUtilsTests(unittest.TestCase):
    def test_encode_and_parse_with_auto_publish(self) -> None:
        raw = encode_auto_youtube_created_by("youtube_home_scan", auto_publish=True)
        parsed = parse_auto_youtube_created_by(raw)

        self.assertEqual(parsed, {"origin": "youtube_home_scan", "auto_publish": True})

    def test_parse_rejects_non_auto_marker(self) -> None:
        self.assertIsNone(parse_auto_youtube_created_by("web"))


if __name__ == "__main__":
    unittest.main()
