from __future__ import annotations

import json
import subprocess

import pytest

from videoroll.apps.dubbing.pipeline import generate_dubbed_media, spoken_segments_from_json


class FakeSynthesizer:
    model_path = "fake:1"

    def __init__(self):
        self.calls = []

    def synthesize(self, text, output, *, voice=None):
        self.calls.append((text, voice))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=300:duration=0.25",
             "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(output)],
            check=True,
        )


def _make_input(tmp_path):
    p1 = tmp_path / "subtitle.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=black:s=320x240:r=25:d=3", "-f", "lavfi",
        "-i", "sine=frequency=100:duration=3", "-t", "3",
        "-c:v", "mpeg4", "-c:a", "aac", str(p1),
    ], check=True)
    segments = tmp_path / "segments.json"
    segments.write_text(json.dumps([
        {"start": 0.5, "end": 1.2, "text": "可阅读的字幕", "speech_text": "朗读文案"},
        {"start": 1.6, "end": 2.3, "text": "第二句中文"},
    ], ensure_ascii=False), encoding="utf-8")
    return p1, segments


def test_pipeline_generates_exact_length_p2_and_reuses_tts_cache(tmp_path):
    p1, subtitles = _make_input(tmp_path)
    provider = FakeSynthesizer()
    workspace = tmp_path / "workspace"
    first = generate_dubbed_media(p1, subtitles, workspace, provider)
    assert first.video_path.is_file()
    assert first.audio_path.is_file()
    assert first.segments_total == 2
    assert len(provider.calls) == 2
    second = generate_dubbed_media(p1, subtitles, workspace, provider)
    assert len(provider.calls) == 2
    assert first.completed_keys == second.completed_keys
    assert first.source_duration == pytest.approx(second.source_duration)


def test_spoken_script_does_not_override_subtitle_text():
    result = spoken_segments_from_json([
        {"start": 1.0, "end": 2.0, "text": "正式的技术字幕", "speech_text": "口播更简洁"},
    ], voice="Vivian")
    assert result[0].subtitle_text == "正式的技术字幕"
    assert result[0].speech_text == "口播更简洁"


def test_pipeline_automatically_clips_last_overlong_subtitle_to_media_boundary(tmp_path):
    p1, subtitles = _make_input(tmp_path)
    original = json.loads(subtitles.read_text(encoding='utf-8'))
    original[-1]['end'] = 4.2  # The source media is only 3 seconds long.
    subtitles.write_text(json.dumps(original,ensure_ascii=False),encoding='utf-8')
    notices=[]
    model=FakeSynthesizer()
    result=generate_dubbed_media(p1,subtitles,tmp_path/'work',model,
                                  on_timing_adjustment=notices.extend)
    assert result.segments_total==2
    assert len(model.calls)==2
    assert notices and notices[0]['segment_index']==2
    assert notices[0]['original_end']==4.2
    assert notices[0]['corrected_end']==pytest.approx(3.0,abs=0.07)
    assert json.loads(subtitles.read_text(encoding='utf-8'))[-1]['end']==4.2
