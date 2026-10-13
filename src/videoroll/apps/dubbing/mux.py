"""Mux a dubbed second Bilibili part from the already rendered subtitle video.

The video bitstream is copied, so producing P2 does not require a second GPU
video encode. The dubbing service is responsible for building a complete
silence-padded audio track with exactly the original video's time base.
"""
from __future__ import annotations

from math import isfinite
from pathlib import Path


def build_dubbed_part_command(
    ffmpeg: str,
    subtitle_video: Path,
    chinese_audio: Path,
    output_video: Path,
    *,
    video_duration: float,
    original_audio_present: bool,
    retain_original_audio: bool = False,
    original_volume: float = 0.10,
) -> list[str]:
    """Return a deterministic FFmpeg command for a separate dubbed P2 MP4.

    retain_original_audio retains a quiet copy of ALL original audio, including
    original speech. It is not a substitute for Demucs source separation and
    should be used only when that tradeoff is explicitly selected.
    """
    if not isfinite(video_duration) or video_duration <= 0:
        raise ValueError("positive video_duration is required")
    if not isfinite(original_volume) or not 0 <= original_volume <= 1:
        raise ValueError("original_volume must be between 0 and 1")
    if retain_original_audio and not original_audio_present:
        raise ValueError("cannot mix absent original audio")
    if subtitle_video == output_video or chinese_audio == output_video:
        raise ValueError("output must be distinct from inputs")
    duration = f"{video_duration:.6f}"
    if retain_original_audio:
        graph = (
            f"[0:a:0]volume={original_volume:.4f}[original];"
            f"[1:a:0]apad=whole_dur={duration}[dub];"
            "[original][dub]amix=inputs=2:duration=longest:normalize=0,"
            "alimiter=limit=0.95[out]"
        )
    else:
        graph = f"[1:a:0]apad=whole_dur={duration},alimiter=limit=0.95[out]"
    return [
        ffmpeg, "-y", "-hide_banner",
        "-i", str(subtitle_video), "-i", str(chinese_audio),
        "-filter_complex", graph,
        "-map", "0:v:0", "-map", "[out]", "-map", "0:s?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-c:s", "copy", "-metadata:s:a:0", "language=zho",
        "-metadata:s:a:0", "title=中文配音",
        "-t", duration, "-movflags", "+faststart", str(output_video),
    ]
