"""Conservatively shorten pathological intra-sentence TTS pauses.

The original model WAV is immutable. Only spans FFmpeg confidently classifies
as continuous low-energy audio for >=1.2s are reduced to a short natural gap.
Speech itself is never sped up or normalized here. The resulting asset has
a separate cache identity and can be compared with the original.
"""
from __future__ import annotations

import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

# TTS with quiet reference-noise floors may have nonzero samples during silence.
# High threshold is tolerable only because the required *continuous* pause is long.
_SILENCE_DB = -26
_MIN_SILENCE_SECONDS = 1.2
_KEEP_SECONDS = 0.18
_VERSION = "long-pause-v1"

_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


@dataclass(frozen=True)
class PauseResult:
    path: Path
    original_seconds: float
    prepared_seconds: float
    removed_seconds: float
    pauses_shortened: int


def prepared_name(cache_key: str) -> str:
    """Keep cleaned versions distinct from immutable model TTS WAVs."""
    return f"{cache_key}.{_VERSION}.wav"


def _ranges(stderr: str, duration: float) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for line in stderr.splitlines():
        match = _SILENCE_START.search(line)
        if match:
            start = float(match.group(1))
        match = _SILENCE_END.search(line)
        if match and start is not None:
            end = min(duration, float(match.group(1)))
            if 0 <= start < end and end - start >= _MIN_SILENCE_SECONDS:
                intervals.append((start, end))
            start = None
    if start is not None and duration - start >= _MIN_SILENCE_SECONDS:
        intervals.append((start, duration))
    return intervals


def prepare_long_pauses(source: Path, destination: Path, *,
                        ffmpeg: str = "ffmpeg") -> PauseResult:
    """Return original if no long low-energy gaps; otherwise make a distinct WAV.

    No generated speech is overwritten. Uses only wave/FFmpeg; refuses unknown
    formats. Idempotent when the caller uses a versioned destination.
    """
    with wave.open(str(source), "rb") as original:
        channels, width, rate = (original.getnchannels(),
                                 original.getsampwidth(), original.getframerate())
        nframes = original.getnframes()
        duration = nframes / rate
        if channels != 1 or width != 2 or rate <= 0:
            return PauseResult(source, duration, duration, 0.0, 0)
        if duration < _MIN_SILENCE_SECONDS:
            return PauseResult(source, duration, duration, 0.0, 0)
    if destination.is_file() and destination.stat().st_size > 44:
        with wave.open(str(destination), "rb") as cleaned:
            if cleaned.getnchannels() == channels and cleaned.getsampwidth() == width and cleaned.getframerate() == rate:
                new_duration = cleaned.getnframes() / rate
                if 0 < new_duration < duration:
                    return PauseResult(destination, duration, new_duration, duration - new_duration, 1)
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(source),
         "-af", f"silencedetect=noise={_SILENCE_DB}dB:d={_MIN_SILENCE_SECONDS}",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=120, check=True,
    )
    spans = _ranges(probe.stderr, duration)
    if not spans:
        return PauseResult(source, duration, duration, 0.0, 0)

    drops: list[tuple[int, int]] = []
    for start, end in spans:
        # Preserve a short part of the pause on both sides of the join,
        # protecting nearby consonants and avoiding abrupt concatenations.
        keep = _KEEP_SECONDS / 2
        begin_frame = round((start + keep) * rate)
        end_frame = round((end - keep) * rate)
        if begin_frame < end_frame and (not drops or begin_frame >= drops[-1][1]):
            drops.append((begin_frame, end_frame))
    removed = sum(end - begin for begin, end in drops)
    if not drops or removed <= 0 or removed > nframes * 0.75:
        return PauseResult(source, duration, duration, 0.0, 0)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".partial")
    try:
        with wave.open(str(source), "rb") as src, wave.open(str(temp), "wb") as dst:
            dst.setparams(src.getparams())
            cursor = 0
            for begin, end in drops:
                src.setpos(cursor)
                remaining = begin - cursor
                while remaining:
                    count = min(remaining, 32768)
                    dst.writeframesraw(src.readframes(count))
                    remaining -= count
                cursor = end
            src.setpos(cursor)
            remaining = nframes - cursor
            while remaining:
                count = min(remaining, 32768)
                dst.writeframesraw(src.readframes(count))
                remaining -= count
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    return PauseResult(destination, duration, (nframes - removed) / rate,
                       removed / rate, len(drops))
