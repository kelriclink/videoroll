from __future__ import annotations

import subprocess
import wave

import pytest

from videoroll.apps.dubbing.audio import assemble_track, wav_duration
from videoroll.apps.dubbing.timing import SpokenSegment, plan_dubbing_timeline, DubbingTimingError


def _synth_wav(path, seconds=0.5):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=600:duration={seconds}",
        "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(path),
    ], check=True)


def test_track_keeps_timestamps_and_exact_duration(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    out = tmp_path / "track.wav"
    _synth_wav(a, 0.5)
    _synth_wav(b, 0.5)
    speech = [
        SpokenSegment(1, 1.0, 1.8, "字幕", "配音", "Vivian"),
        SpokenSegment(2, 2.0, 2.8, "字幕", "配音", "Vivian"),
    ]
    places = plan_dubbing_timeline(speech, {1: wav_duration(a), 2: wav_duration(b)}, video_duration=3)
    assemble_track(places, {1: a, 2: b}, out, video_duration=3)
    assert wav_duration(out) == pytest.approx(3, abs=1 / 24000)
    with wave.open(str(out), "rb") as audio:
        assert audio.readframes(24000) == b"\x00\x00" * 24000
        assert audio.getnframes() == 72000


def test_track_fails_for_missing_tts_clip(tmp_path):
    p = plan_dubbing_timeline([SpokenSegment(1, 0, 1, "字幕", "配音", "Vivian")], {1: 0.5}, video_duration=2)
    with pytest.raises(DubbingTimingError, match="missing"):
        assemble_track(p, {}, tmp_path / "track.wav", video_duration=2)
