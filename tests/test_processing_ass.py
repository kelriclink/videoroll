from __future__ import annotations

import sys
import types
import unittest

try:
    import httpx as _httpx  # type: ignore
except ModuleNotFoundError:
    fake_httpx = types.ModuleType("httpx")

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    fake_httpx.Client = Client
    sys.modules["httpx"] = fake_httpx

from videoroll.apps.subtitle_service.processing import Segment, segments_to_ass


class ProcessingAssTests(unittest.TestCase):
    def test_segments_to_ass_uses_larger_primary_and_smaller_secondary_fonts(self) -> None:
        ass_text = segments_to_ass(
            [
                Segment(
                    start=0.0,
                    end=2.0,
                    text="这是一行中文主字幕",
                    secondary_text="This is a longer English secondary subtitle line.",
                )
            ],
            play_res_x=1920,
            play_res_y=1080,
            secondary_line_scale=0.68,
        )

        self.assertIn("Style: Default,Noto Sans CJK SC,58,", ass_text)
        self.assertIn("Style: Secondary,Noto Sans CJK SC,39,", ass_text)

    def test_segments_to_ass_applies_font_scale_percent_over_current_defaults(self) -> None:
        ass_text = segments_to_ass(
            [
                Segment(
                    start=0.0,
                    end=2.0,
                    text="主字幕",
                    secondary_text="Secondary subtitle",
                )
            ],
            play_res_x=1920,
            play_res_y=1080,
            secondary_line_scale=0.68,
            primary_font_scale_percent=150,
            secondary_font_scale_percent=125,
        )

        self.assertIn("Style: Default,Noto Sans CJK SC,87,", ass_text)
        self.assertIn("Style: Secondary,Noto Sans CJK SC,49,", ass_text)


if __name__ == "__main__":
    unittest.main()
