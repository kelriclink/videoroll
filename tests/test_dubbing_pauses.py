"""Regression for CosyVoice3 audio with multi-second false pauses."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import wave
from pathlib import Path

import pytest

from videoroll.apps.dubbing.audio import wav_duration
from videoroll.apps.dubbing.pauses import prepare_long_pauses, prepared_name
from videoroll.apps.dubbing.pipeline import generate_dubbed_media
from videoroll.apps.dubbing.timing import SpokenSegment, check_segment_fit


def _abnormally_paused_wav(path: Path, *, sample_rate: int = 24000) -> None:
    # Speech-like runs separated by model hallucinated noise floor (-36dB RMS).
    # Avoid exact zeros: older checks for 'silence' cannot catch these.
    parts = [
        (1.3, 0.25), (3.6, 0.016),
        (2.5, 0.22), (4.6, 0.014),
        (1.2, 0.2),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(sample_rate)
        for length, amplitude in parts:
            frames = round(length * sample_rate)
            chunk = bytearray()
            for i in range(frames):
                value = int(32767 * amplitude * math.sin(2 * math.pi * 230 * i / sample_rate))
                chunk.extend(value.to_bytes(2, 'little', signed=True))
            dst.writeframesraw(chunk)


def test_two_pathological_pauses_removed_original_preserved(tmp_path):
    original = tmp_path / 'model.wav'
    _abnormally_paused_wav(original)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    prepared = tmp_path / prepared_name('a' * 64)
    result = prepare_long_pauses(original, prepared)
    assert result.pauses_shortened >= 2
    assert 5 <= result.prepared_seconds <= 7
    assert 13 <= result.original_seconds <= 14
    assert result.removed_seconds > 6
    assert result.path != original
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest

    segments = [
        SpokenSegment(1, 1, 5.44, '字幕', '我们的输入先接到一个 OP16F2。', 'reference'),
        SpokenSegment(2, 5.44, 6.44, '下一句', '下一句', 'reference'),
    ]
    assert check_segment_fit(segments, 0, result.prepared_seconds,
                             video_duration=8, max_speed=2.5) < 2.5


def test_normal_short_speech_remains_unmodified(tmp_path):
    original = tmp_path / 'short.wav'
    subprocess.run(
        ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi',
         '-i', 'sine=frequency=300:duration=1.5',
         '-ar', '24000', '-ac', '1', str(original)],
        check=True,
    )
    result = prepare_long_pauses(original, tmp_path / 'clean.wav')
    assert result.path == original
    assert result.pauses_shortened == 0
    assert not (tmp_path / 'clean.wav').exists()


def test_pipeline_applies_prepared_audio_to_timeline_and_preserves_raw(tmp_path):
    video = tmp_path / 'video.mp4'
    subprocess.run(
        ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi',
         '-i', 'color=black:s=160x90:d=8:r=25',
         '-f', 'lavfi', '-i', 'sine=frequency=300:duration=8',
         '-c:v', 'mpeg4', '-c:a', 'aac', str(video)],
        check=True,
    )
    subtitles = tmp_path / 'captions.json'
    subtitles.write_text(json.dumps([
        {'start': 1.0, 'end': 5.44, 'text': '我们的输入先接到一个 OP16F2。'},
        {'start': 5.44, 'end': 6.44, 'text': '运算放大器。'},
    ], ensure_ascii=False))
    calls = []

    class Tts:
        model_path = 'cosyvoice3:base'

        def synthesize(self, text, output, *, voice=None):
            calls.append(text)
            if len(calls) == 1:
                _abnormally_paused_wav(output)
            else:
                subprocess.run(
                    ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi',
                     '-i', 'sine=frequency=400:duration=0.6',
                     '-ar', '24000', '-ac', '1', str(output)],
                    check=True,
                )

    prepared_reports = []
    settings = {'max_audio_speed': 2.5, 'auto_group_speech': False}
    workspace = tmp_path / 'workspace'
    result = generate_dubbed_media(
        video, subtitles, workspace, Tts(), voice='reference',
        quality=settings,
        on_segment_prepared=lambda key, path, report: prepared_reports.append((key, path, report)),
    )
    assert result.audio_path.is_file()
    assert result.video_path.is_file()
    assert result.source_duration == pytest.approx(8.0, abs=0.1)
    assert len(calls) == 2
    assert len(prepared_reports) == 1
    assert prepared_reports[0][2].removed_seconds > 6
    first = next(iter(result.completed_keys.values()))
    raw = workspace / 'segments' / f'{first}.wav'
    assert wav_duration(raw) > 13
    # The gap-preserving finished mix can be generated from the shortened WAV.
    assert wav_duration(result.audio_path) == pytest.approx(8, abs=1 / 24000)
    # Rerun must reuse raw clips and deterministic prepared assets.
    result2 = generate_dubbed_media(video, subtitles, workspace, Tts(), voice='reference',
                                    quality=settings)
    assert len(calls) == 2
    assert result2.source_duration == pytest.approx(8.0, abs=0.1)
