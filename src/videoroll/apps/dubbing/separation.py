"""Optional Demucs source-separation adapter; outputs are audio-only assets."""
from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path

def available() -> bool:
    name = os.environ.get("DUBBING_DEMUCS_BIN", "demucs")
    return bool(shutil.which(name) or (Path(name).is_file() and os.access(name, os.X_OK)))

def background_stem(video: Path, workspace: Path, *,
                    ffmpeg: str = "ffmpeg") -> Path:
    """Extract audio first; Demucs does not take MP4 video as an input."""
    bin = os.environ.get("DUBBING_DEMUCS_BIN", "demucs")
    if not available():
        raise RuntimeError("Demucs vocal separation is not installed or not enabled")
    workspace.mkdir(parents=True, exist_ok=True)
    audio = workspace / "original-44100-stereo.wav"
    stem = workspace / "separated" / "htdemucs" / audio.stem / "no_vocals.wav"
    if stem.is_file() and stem.stat().st_size > 44:
        return stem
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(video), "-vn", "-ac", "2", "-ar", "44100",
                    "-c:a", "pcm_s16le", str(audio)], check=True, timeout=3600,
                   capture_output=True)
    subprocess.run([bin, "--two-stems=vocals", "-n", "htdemucs",
                    "--out", str(workspace / "separated"), str(audio)],
                   check=True, timeout=7200, capture_output=True)
    if not stem.is_file() or stem.stat().st_size <= 44:
        raise RuntimeError("Demucs did not produce a background-only stem")
    return stem
