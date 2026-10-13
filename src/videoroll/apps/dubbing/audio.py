"""Construct one gap-preserving PCM audio track from aligned TTS clips.

All intervals are in the source video's timeline. The source video is never
stretched or trimmed. Process each clip independently with FFmpeg and stream
PCM to a WAV writer to avoid holding long videos in RAM.
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Mapping, Sequence

from videoroll.apps.dubbing.timing import AudioPlacement, DubbingTimingError


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        if wav.getnframes() <= 0 or wav.getframerate() <= 0:
            raise DubbingTimingError(f"empty audio file: {path}")
        return wav.getnframes() / wav.getframerate()


def _write_silence(writer: wave.Wave_write, frames: int, channels: int = 1) -> None:
    # Chunked to avoid very large allocations on 2-hour tutorial videos.
    while frames > 0:
        count = min(frames, 24000)
        writer.writeframesraw(b"\x00\x00" * (count * channels))
        frames -= count


def assemble_track(
    placements: Sequence[AudioPlacement],
    audio_files: Mapping[int, Path],
    output: Path,
    *,
    video_duration: float,
    ffmpeg_path: str = "ffmpeg",
    sample_rate: int = 24000,
) -> None:
    if video_duration <= 0 or sample_rate not in (24000, 48000):
        raise ValueError("unsupported audio timeline")
    output.parent.mkdir(parents=True, exist_ok=True)
    total_frames = round(video_duration * sample_rate)
    cursor = 0
    with wave.open(str(output), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        for placement in placements:
            path = audio_files.get(placement.index)
            if path is None or not path.is_file():
                raise DubbingTimingError(f"segment {placement.index}: audio missing")
            start = round(placement.start * sample_rate)
            deadline = round(placement.deadline * sample_rate)
            if start < cursor or deadline > total_frames or deadline < start:
                raise DubbingTimingError(f"segment {placement.index}: overlaps existing audio")
            _write_silence(out, start - cursor)
            cursor = start
            # Decode one clip to bounded-size raw audio on disk. atempo is
            # controlled by the validated timing plan (1.0 .. 1.25).
            raw_path = output.with_name(f"{output.stem}_{placement.index}.pcm.tmp")
            try:
                command = [
                    ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(path), "-vn",
                    "-af", f"atempo={placement.playback_speed:.9f},aresample={sample_rate}",
                    "-ac", "1", "-ar", str(sample_rate), "-f", "s16le",
                    str(raw_path),
                ]
                subprocess.run(command, check=True, capture_output=True, timeout=600)
                count = raw_path.stat().st_size // 2
                if count == 0 or count > deadline - start + round(0.025 * sample_rate):
                    raise DubbingTimingError(
                        f"segment {placement.index}: decoded audio exceeds slot; retry shorter speech"
                    )
                if count > deadline - start:
                    # FFmpeg resampler may add up to 25ms of padding. Don't
                    # truncate speech content: fail and request regeneration.
                    raise DubbingTimingError(f"segment {placement.index}: decoding overshot deadline")
                with raw_path.open("rb") as src:
                    while chunk := src.read(128 * 1024):
                        out.writeframesraw(chunk)
                cursor += count
            finally:
                raw_path.unlink(missing_ok=True)
        if cursor > total_frames:
            raise DubbingTimingError("audio track exceeds video duration")
        _write_silence(out, total_frames - cursor)
