"""Bounded dubbing timing decisions, without changing source-video timing.

Speech generation is upstream of this module. A spoken segment may use a more
concise script than its readable subtitle; both versions remain separate.
Segments that exceed the allowed speed-up must be rewritten or regenerated,
never silently truncated, overlapped or allowed to lengthen the video.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
import re
from typing import Mapping, Sequence


class DubbingTimingError(ValueError):
    """A spoken segment cannot safely fit in the original video timeline."""


class SpeechTooLong(DubbingTimingError):
    def __init__(self, index: int, duration: float, slot: float, required_speed: float,
                 max_speed: float = 1.25):
        self.index, self.duration, self.slot = index, duration, slot
        self.required_speed = required_speed
        super().__init__(f"segment {index}: speech {duration:.3f}s exceeds slot {slot:.3f}s "
                         f"(requires {required_speed:.2f}x; maximum {max_speed:.2f}x). "
                         "Shorten this spoken script and regenerate this segment.")


def estimate_spoken_seconds(text: str) -> float:
    """Rough preflight-only Mandarin duration estimate, NOT a timing decision.

    Only measured WAV lengths determine acceptance. This advisory estimate
    finds obvious short slots before paying for long-running TTS.
    """
    han = sum('\u3400' <= c <= '\u9fff' for c in text)
    latin = len(re.findall(r'[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*', text))
    pauses = sum(c in '，。！？；：,.!?;:' for c in text)
    return round(max(0.1, han / 4.5 + latin / 2.7 + pauses * 0.10), 3)


def speech_slot_seconds(segments: Sequence[SpokenSegment], position: int, *,
                        video_duration: float, max_extension: float = 0.5,
                        min_gap: float = 0.08) -> float:
    part = segments[position]
    next_start = segments[position + 1].start if position + 1 < len(segments) else video_duration + min_gap
    return max(0.0, min(part.end + max_extension, next_start - min_gap, video_duration) - part.start)


def check_segment_fit(segments: Sequence[SpokenSegment], index: int, duration: float,
                      *, video_duration: float, max_extension: float = 0.5,
                      min_gap: float = 0.08, max_speed: float = 1.25) -> float:
    """Immediately validate each generated clip against the next speech start."""
    part = segments[index]
    next_start = segments[index + 1].start if index + 1 < len(segments) else video_duration + min_gap
    deadline = min(part.end + max_extension, next_start - min_gap, video_duration)
    slot = deadline - part.start
    if slot <= 0:
        raise DubbingTimingError(f"segment {part.index}: no audio slot available")
    speed = max(1.0, duration / slot)
    if speed > max_speed + 1e-8:
        raise SpeechTooLong(part.index, duration, slot, speed, max_speed)
    return speed


@dataclass(frozen=True, slots=True)
class SpokenSegment:
    index: int
    start: float
    end: float
    subtitle_text: str
    speech_text: str
    voice: str


def reconcile_final_subtitle_boundary(
    segments: Sequence[SpokenSegment], *, video_duration: float,
    max_tail_overhang: float = 30.0,
) -> tuple[list[SpokenSegment], list[dict[str, float | int | str]]]:
    """Clip a final ASR/translation end time that overshoots source media.

    Only the last segment may be corrected automatically. Earlier malformed
    timestamps, zero/negative intervals, entries fully outside the video and
    implausibly large overruns still fail the strict timeline preflight.
    Preserve original segment indices, texts and cache keys; don't modify SRT.
    """
    result = list(segments)
    if not result or not isfinite(video_duration) or video_duration <= 0:
        return result, []
    last = result[-1]
    overrun = last.end - video_duration
    if overrun <= 1e-8:
        return result, []
    if (not isfinite(last.start) or not isfinite(last.end)
        or last.start < 0 or last.start >= video_duration - 0.1
        or last.end <= last.start or overrun > max_tail_overhang):
        return result, []  # Retain the hard error with segment index.
    result[-1] = replace(last, end=video_duration)
    return result, [{
        'segment_index': last.index, 'reason': 'final_subtitle_past_video_end',
        'original_end': round(last.end, 3),
        'corrected_end': round(video_duration, 3),
        'overrun_seconds': round(overrun, 3),
    }]


@dataclass(frozen=True, slots=True)
class AudioPlacement:
    index: int
    start: float
    deadline: float
    source_duration: float
    playback_speed: float
    actual_duration: float

    @property
    def finish(self) -> float:
        return self.start + self.actual_duration


def plan_dubbing_timeline(
    segments: Sequence[SpokenSegment],
    audio_durations: Mapping[int, float],
    *,
    video_duration: float,
    max_extension: float = 0.5,
    min_gap: float = 0.08,
    max_speed: float = 1.25,
) -> list[AudioPlacement]:
    """Place measured TTS audio in nonoverlapping slots of the original video.

    The deadline may extend beyond the subtitle end into a small silence gap,
    but never beyond the next segment or the total video duration. If more
    than max_speed acceleration is necessary, raise instead of clipping
    speech. The caller can request a shorter spoken rewrite and retry.
    """
    if not isfinite(video_duration) or video_duration <= 0:
        raise DubbingTimingError("video duration must be positive and finite")
    if not isfinite(max_speed) or not 1.0 <= max_speed <= 2.5:
        raise DubbingTimingError("max_speed must be between 1.0 and 2.5")
    if not isfinite(max_extension) or max_extension < 0:
        raise DubbingTimingError("max_extension must be nonnegative")
    if not isfinite(min_gap) or min_gap < 0:
        raise DubbingTimingError("min_gap must be nonnegative")

    placements: list[AudioPlacement] = []
    seen_indices: set[int] = set()
    previous_end = 0.0

    for i, segment in enumerate(segments):
        if segment.index in seen_indices:
            raise DubbingTimingError(f"duplicate segment index {segment.index}")
        seen_indices.add(segment.index)
        if not all(isfinite(x) for x in (segment.start, segment.end)):
            raise DubbingTimingError(f"segment {segment.index}: nonfinite source timestamps")
        if segment.start < previous_end - 1e-8 or segment.start < 0:
            raise DubbingTimingError(f"segment {segment.index}: overlapping or unordered source")
        if segment.end <= segment.start or segment.end > video_duration + 1e-8:
            reason = ('nonpositive interval' if segment.end <= segment.start
                      else 'extends past media end')
            raise DubbingTimingError(
                f'segment {segment.index}: invalid source interval ({reason}; '
                f'start={segment.start:.3f}s end={segment.end:.3f}s '
                f'video={video_duration:.3f}s). Recheck ASR/subtitle boundaries'
            )
        if not segment.speech_text.strip():
            raise DubbingTimingError(f"segment {segment.index}: empty spoken text")
        previous_end = segment.end

        duration = audio_durations.get(segment.index)
        if duration is None or not isfinite(duration) or duration <= 0:
            raise DubbingTimingError(f"segment {segment.index}: invalid TTS duration")

        next_start = segments[i + 1].start if i + 1 < len(segments) else video_duration + min_gap
        deadline = min(segment.end + max_extension, next_start - min_gap, video_duration)
        # There is always room for the original subtitle interval. A very
        # short gap might force speech to end slightly before subtitle end.
        slot_duration = deadline - segment.start
        if slot_duration <= 0:
            raise DubbingTimingError(f"segment {segment.index}: no audio slot available")
        necessary_speed = max(1.0, duration / slot_duration)
        if necessary_speed > max_speed + 1e-8:
            raise DubbingTimingError(
                f"segment {segment.index}: speech {duration:.3f}s exceeds "
                f"slot {slot_duration:.3f}s at max_speed {max_speed:.2f}; "
                "rewrite speech_text or regenerate audio"
            )
        # Leave a tiny FFmpeg tempo-filter safety margin for very fast clips:
        # atempo can differ by a few milliseconds from the ideal duration.
        # The adjustment is bounded by the operator-selected maximum.
        playback_speed = (min(max_speed, necessary_speed * 1.015)
                          if necessary_speed > 2.0 else necessary_speed)
        actual_duration = duration / playback_speed
        placements.append(
            AudioPlacement(
                index=segment.index,
                start=segment.start,
                deadline=deadline,
                source_duration=duration,
                playback_speed=playback_speed,
                actual_duration=actual_duration,
            )
        )
    return placements
