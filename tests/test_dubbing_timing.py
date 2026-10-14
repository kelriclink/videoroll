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


def test_reconcile_final_subtitle_overrun_preserves_source_and_index() -> None:
    from videoroll.apps.dubbing.timing import reconcile_final_subtitle_boundary
    segments = [_segment(1, 0.5, 5.0), _segment(278, 1264.88, 1294.86)]
    fixed, adjustments = reconcile_final_subtitle_boundary(segments, video_duration=1282.081)
    assert segments[-1].end == 1294.86  # Original subtitle/translation remains untouched.
    assert fixed[0] == segments[0]
    assert fixed[1].index == 278
    assert fixed[1].start == 1264.88
    assert fixed[1].end == pytest.approx(1282.081)
    assert adjustments == [{
        'segment_index': 278, 'reason': 'final_subtitle_past_video_end',
        'original_end': 1294.86, 'corrected_end': 1282.081,
        'overrun_seconds': 12.779,
    }]


@pytest.mark.parametrize('start,end,duration', [
    (1300, 1320, 1282),  # wholly beyond video
    (1279.99, 1294, 1280),  # too little valid tail remains
    (1250, 1330, 1280),  # malformed by over 30 seconds
    (20, 19, 1280),  # negative interval
])
def test_reconcile_never_silently_repairs_gross_errors(start,end,duration):
    from videoroll.apps.dubbing.timing import reconcile_final_subtitle_boundary
    original=[_segment(1,start,end)]
    fixed,changes=reconcile_final_subtitle_boundary(original,video_duration=duration)
    assert fixed==original
    assert not changes


def test_reconcile_does_not_change_nonfinal_bad_segments():
    from videoroll.apps.dubbing.timing import reconcile_final_subtitle_boundary
    original=[_segment(1,1264,1294),_segment(2,1275,1278)]
    fixed, changes=reconcile_final_subtitle_boundary(original,video_duration=1282)
    assert fixed == original and not changes
