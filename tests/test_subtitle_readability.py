from __future__ import annotations

from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.subtitle_readability import optimize_subtitle_readability


def test_readability_wraps_long_cjk_into_at_most_two_lines() -> None:
    result = optimize_subtitle_readability(
        [Segment(start=0.0, end=4.0, text="这是一个用于测试字幕可读性优化器的比较长的中文字幕句子")],
        max_line_units=12.0,
    )

    assert result.stats.input_segments == 1
    assert result.stats.wrapped_segments == 1
    assert all(item.text.count("\n") <= 1 for item in result.segments)


def test_readability_prefers_punctuation_when_event_must_split() -> None:
    result = optimize_subtitle_readability(
        [
            Segment(
                start=0.0,
                end=5.0,
                text="This is the first clause, and this is the second clause, followed by a final phrase.",
            )
        ],
        max_line_units=10.0,
    )

    assert len(result.segments) >= 2
    assert result.stats.split_segments == 1
    assert result.segments[0].text.replace("\n", " ").rstrip().endswith(",")
    assert result.segments[-1].end == 5.0


def test_readability_extends_short_caption_into_available_gap() -> None:
    result = optimize_subtitle_readability(
        [
            Segment(start=0.0, end=0.45, text="short readable caption"),
            Segment(start=2.0, end=3.0, text="next"),
        ],
        target_cps=12.0,
        min_display_seconds=0.9,
        max_gap_extension_seconds=0.75,
    )

    assert result.segments[0].end > 0.45
    assert result.segments[0].end <= 1.20
    assert result.segments[0].end < result.segments[1].start
    assert result.stats.extended_segments == 1


def test_readability_does_not_extend_into_next_caption() -> None:
    result = optimize_subtitle_readability(
        [
            Segment(start=0.0, end=0.45, text="very long caption that needs more time"),
            Segment(start=0.55, end=1.5, text="next"),
        ],
        target_cps=10.0,
        min_display_seconds=1.0,
    )

    assert result.segments[0].end <= 0.50
    assert result.segments[0].end < result.segments[1].start


def test_bilingual_readability_preserves_one_to_one_timing_and_wraps_both_lines() -> None:
    source = Segment(
        start=1.0,
        end=4.0,
        text="这是一段相当长的中文字幕，需要在显示的时候进行平衡断行",
        secondary_text="This is a fairly long source subtitle that should also be balanced for display.",
    )

    result = optimize_subtitle_readability([source], bilingual=True, max_line_units=12.0)

    assert len(result.segments) == 1
    segment = result.segments[0]
    assert (segment.start, segment.end) == (1.0, 4.0)
    assert "\n" in segment.text
    assert segment.secondary_text is not None
    assert segment.secondary_text.count("\n") <= 1


def test_readability_avoids_too_many_splits_for_very_short_event() -> None:
    source = Segment(
        start=0.0,
        end=0.8,
        text="This is an extremely long subtitle that would normally require several display events.",
    )

    result = optimize_subtitle_readability([source], max_line_units=8.0)

    assert len(result.segments) == 1
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 0.8
