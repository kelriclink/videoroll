from __future__ import annotations

import pytest

from videoroll.apps.dubbing.timing import (
    DubbingTimingError,
    SpokenSegment,
    plan_dubbing_timeline,
)


def _segment(index: int, start: float, end: float) -> SpokenSegment:
    return SpokenSegment(index, start, end, "阅读用中文字幕", "朗读用中文台词", "Vivian")


def test_shorter_tts_uses_original_speed_without_extending_video() -> None:
    placements = plan_dubbing_timeline(
        [_segment(1, 1, 3), _segment(2, 4, 6)],
        {1: 1.5, 2: 1.0}, video_duration=8,
    )
    assert [p.playback_speed for p in placements] == [1.0, 1.0]
    assert placements[0].start == 1
    assert placements[-1].finish < 8


def test_tts_may_use_small_silence_gap_and_safe_acceleration() -> None:
    placements = plan_dubbing_timeline(
        [_segment(1, 1, 3), _segment(2, 4, 6)],
        {1: 2.8, 2: 2.0}, video_duration=7,
    )
    assert placements[0].deadline == pytest.approx(3.5)
    assert placements[0].playback_speed == pytest.approx(2.8 / 2.5)
    assert placements[0].finish <= placements[1].start - 0.08 + 1e-8


def test_overlong_tts_requests_rewrite_rather_than_truncation() -> None:
    with pytest.raises(DubbingTimingError, match="rewrite speech_text"):
        plan_dubbing_timeline([_segment(1, 1, 3)], {1: 4.0}, video_duration=4)


@pytest.mark.parametrize("segments", [
    [_segment(1, 2, 4), _segment(2, 3, 5)],
    [_segment(1, 1, 4), _segment(2, 0, 2)],
    [_segment(1, 1, 6)],
])
def test_invalid_source_timing_is_rejected(segments: list[SpokenSegment]) -> None:
    with pytest.raises(DubbingTimingError):
        plan_dubbing_timeline(segments, {1: 1, 2: 1}, video_duration=5)


def test_original_video_duration_is_hard_bound() -> None:
    placement, = plan_dubbing_timeline([_segment(1, 3, 4)], {1: 1.1}, video_duration=4)
    assert placement.finish == pytest.approx(4.0)
    assert placement.playback_speed == pytest.approx(1.1)
