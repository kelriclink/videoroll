"""Full VideoRoll -> upstream YouDub alignment -> MP4 smoke tests.

The input intentionally has several lines whose synthesized voice cannot fit
the original subtitle slots. The former VideoRoll renderer failed at the first.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from videoroll.apps.dubbing.pipeline import generate_dubbed_media
from videoroll.apps.dubbing.audio import wav_duration


def _duration(path, *, selector='format=duration'):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', selector,
        '-of', 'default=noprint_wrappers=1:nokey=1', str(path),
    ], check=True, capture_output=True, text=True)
    return float(result.stdout.strip().splitlines()[0])


def test_youdub_replaces_clip_deadlines_and_extends_video(tmp_path):
    video = tmp_path / 'source.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
        '-i', 'color=c=blue:s=160x90:r=20:d=3',
        '-f', 'lavfi', '-i', 'sine=frequency=250:duration=3',
        '-t', '3', '-c:v', 'mpeg4', '-c:a', 'aac', str(video),
    ], check=True)
    subtitles = tmp_path / 'subtitles.json'
    segments = [
        {'start': 0.2 + i * 0.42, 'end': 0.42 + i * 0.42,
         'text': f'第{i+1}句，这是明显超过时间窗的配音。'}
        for i in range(4)
    ]
    subtitles.write_text(json.dumps(segments, ensure_ascii=False))

    class OversizedTTS:
        model_path = 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'

        def __init__(self):
            self.calls = 0

        def synthesize(self, text, output, *, voice=None):
            self.calls += 1
            subprocess.run([
                'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                '-i', 'sine=frequency=300:duration=1.6',
                '-ar', '24000', '-ac', '1', '-c:a', 'pcm_s16le',
                str(output),
            ], check=True)

    model = OversizedTTS()
    work = tmp_path / 'job'
    result = generate_dubbed_media(video, subtitles, work, model, voice='Vivian',
                                   quality={'auto_group_speech': False})
    assert model.calls == 4
    assert result.segments_total == 4
    assert result.video_path.is_file()
    assert result.audio_path.is_file()
    assert result.source_duration > 3.4
    assert wav_duration(result.audio_path) > 3.4
    assert _duration(result.video_path) >= result.source_duration - 0.15
    # Ensure *video stream* spans the extended audio, not just container tags.
    assert _duration(result.video_path, selector='stream=duration') >= (
        result.source_duration - 0.15
    )
    timing = json.loads((work / 'youdub' / 'metadata' / 'timings.json').read_text())
    assert len(timing['translation']) == 4
    assert timing['translation'][1]['actual_start_time'] > (
        timing['translation'][1]['start_time']
    )
    # Idempotent resume: no discarded existing Qwen/CosyVoice TTS generation.
    again = generate_dubbed_media(video, subtitles, work, model, voice='Vivian',
                                  quality={'auto_group_speech': False})
    assert model.calls == 4
    assert again.completed_keys == result.completed_keys
    assert again.source_duration == pytest.approx(result.source_duration, abs=0.2)
