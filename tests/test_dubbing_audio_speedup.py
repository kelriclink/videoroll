"""Measured high-tempo dubbing fits short source windows without mutating TTS."""
from __future__ import annotations

import wave
import pytest

from videoroll.apps.dubbing.audio import assemble_track, wav_duration
from videoroll.apps.dubbing.timing import (
    SpokenSegment, SpeechTooLong, check_segment_fit, plan_dubbing_timeline,
)
from videoroll.apps.dubbing.pipeline import segment_cache_key
from videoroll.apps.dubbing.quality import normalize_quality
from tests.test_dubbing_audio import _synth_wav


def _parts():
    return [
        SpokenSegment(3, 17.66, 19.02, '字幕3', '我又不是科学家。', 'reference'),
        SpokenSegment(4, 19.02, 20.16, '字幕4', '我们也不是科学家，我懂。', 'reference'),
        SpokenSegment(5, 20.16, 22.88, '字幕5', '天呐，数字设计简单多了。', 'reference'),
    ]


def test_real_failed_segment_can_reuse_tts_audio_with_bounded_tempo(tmp_path):
    parts = _parts()
    durations = {3: 1.0, 4: 2.28, 5: 1.9}
    # Existing 2x setting necessarily fails the reported 1.06s slot.
    with pytest.raises(SpeechTooLong):
        check_segment_fit(parts, 1, 2.28, video_duration=24, max_speed=2.0)
    expected = check_segment_fit(parts, 1, 2.28, video_duration=24, max_speed=2.25)
    assert expected == pytest.approx(2.28 / 1.06, abs=0.0001)
    placements = plan_dubbing_timeline(parts, durations, video_duration=24, max_speed=2.25)
    assert placements[1].playback_speed > 2.0
    assert placements[1].playback_speed <= 2.25
    assert placements[1].deadline == pytest.approx(20.08)
    assert placements[1].finish < 20.08

    files = {}
    for segment in parts:
        path = tmp_path / f'{segment.index}.wav'
        _synth_wav(path, durations[segment.index])
        files[segment.index] = path
    destination = tmp_path / 'dub.wav'
    assemble_track(placements, files, destination, video_duration=24)
    assert wav_duration(destination) == pytest.approx(24, abs=1 / 24000)
    with wave.open(str(destination), 'rb') as audio:
        audio.setpos(round(20.10 * 24000))
        # There must be no residual segment-4 audio within the mandatory gap.
        assert audio.readframes(480) == bytes(960)


def test_high_speed_never_accepts_unbounded_overrun():
    parts = _parts()
    with pytest.raises(SpeechTooLong):
        check_segment_fit(parts, 1, 2.88, video_duration=24, max_speed=2.5)
    with pytest.raises(ValueError):
        normalize_quality(max_audio_speed=3.0)


def test_tempo_only_change_does_not_invalidate_speech_cache():
    seg = _parts()[1]
    old = normalize_quality(model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice',
                            max_audio_speed=2.0)
    new = normalize_quality(model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice',
                            max_audio_speed=2.25)
    assert segment_cache_key(seg, 'model', quality=old) == segment_cache_key(
        seg, 'model', quality=new)


def test_ffmpeg_tempo_rechecks_measured_frames_without_truncation(tmp_path, monkeypatch):
    from videoroll.apps.dubbing.timing import AudioPlacement
    from videoroll.apps.dubbing import audio as module

    input_wav = tmp_path / 'original.wav'
    _synth_wav(input_wav, 2.28)
    calls = []

    def measured_run(argv, **_kwargs):
        calls.append(argv)
        # Emulate atempo's non-monotonic block rounding: first result
        # is 10ms over, second is 20ms short. No audio may be truncated.
        frames = round((1.07 if len(calls) == 1 else 1.04) * 24000)
        from pathlib import Path
        Path(argv[-1]).write_bytes(bytes(frames * 2))

    monkeypatch.setattr(module.subprocess, 'run', measured_run)
    p = AudioPlacement(index=4, start=0, deadline=1.06,
                       source_duration=2.28, playback_speed=2.18,
                       actual_duration=2.28 / 2.18)
    track = tmp_path / 'track.wav'
    assemble_track([p], {4: input_wav}, track, video_duration=2,
                   max_playback_speed=2.25)
    assert len(calls) == 2
    assert calls[0][calls[0].index('-af') + 1].count('atempo=') == 2
    assert wav_duration(track) == pytest.approx(2, abs=1 / 24000)
    with wave.open(str(track), 'rb') as f:
        f.setpos(round(1.05 * 24000))
        assert f.readframes(480) == bytes(960)


def test_audio_tempo_refuses_to_exceed_selected_ceiling(tmp_path, monkeypatch):
    from videoroll.apps.dubbing.timing import AudioPlacement, DubbingTimingError
    from videoroll.apps.dubbing import audio as module

    input_wav = tmp_path / 'original.wav'
    _synth_wav(input_wav, 2.28)

    def always_overrun(argv, **_kwargs):
        from pathlib import Path
        Path(argv[-1]).write_bytes(bytes(round(1.2 * 24000) * 2))

    monkeypatch.setattr(module.subprocess, 'run', always_overrun)
    p = AudioPlacement(index=4, start=0, deadline=1.06,
                       source_duration=2.28, playback_speed=2.25,
                       actual_duration=2.28 / 2.25)
    with pytest.raises(DubbingTimingError, match='max 2.25x'):
        assemble_track([p], {4: input_wav}, tmp_path / 'out.wav',
                       video_duration=2, max_playback_speed=2.25)
