"""Regression coverage for opted-in duration-aware spoken-script repair.

These tests use deterministic synthetic WAVs. They never call a live LLM or
modify translated captions, and assert the rerun cache remains reusable.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from videoroll.apps.dubbing.pipeline import generate_dubbed_media
from videoroll.apps.dubbing.rewrite import safe_automatic_rewrite
from videoroll.apps.dubbing.speech_units import (
    apply_speech_unit_overrides, build_speech_units, speech_fingerprint,
)
from videoroll.apps.dubbing.pipeline import spoken_segments_from_json
from videoroll.apps.dubbing.timing import SpeechTooLong
from tests.test_dubbing_pipeline import _make_input


class TimedSynth:
    model_path = "fake:duration-control"

    def __init__(self):
        self.calls: list[str] = []

    def synthesize(self, text, output, *, voice=None):
        self.calls.append(text)
        seconds = 1.5 if text == "我们也不是科学家，我懂。" else 0.3
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"sine=frequency=400:duration={seconds}",
             "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(output)],
            check=True,
        )


def test_auto_rewrite_once_then_restore_exact_speech_unit_cache(tmp_path):
    video, source = _make_input(tmp_path)
    original = [
        {"start": 0.2, "end": 0.7, "text": "我们也不是科学家，我懂。"},
        {"start": 0.9, "end": 1.8, "text": "后面是下一句话。"},
    ]
    source.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    model = TimedSynth()
    proposals = []
    events = []
    quality = {"auto_rewrite_speech": True}
    units = build_speech_units(spoken_segments_from_json(original, voice="Vivian"), enabled=False)
    saved = {}

    def propose(unit, target_seconds, original_seconds):
        proposals.append((unit.part.index, target_seconds, original_seconds))
        candidate = "我懂，我们也不是专家。"
        saved[str(unit.part.index)] = {
            "fingerprint": speech_fingerprint(unit),
            "source_indices": list(unit.source_indices),
            "speech_text": candidate,
        }
        return candidate

    result = generate_dubbed_media(
        video, source, tmp_path / "work", model, quality=quality,
        on_auto_rewrite=propose, on_speech_units=lambda items: events.append(items),
    )
    assert result.video_path.is_file()
    # The legacy repair callback is deliberately never called by YouDub:
    # audio is scheduled without silently editing the translated speech.
    assert proposals == []
    assert model.calls == ["我们也不是科学家，我懂。", "后面是下一句话。"]
    assert events[-1][0]["speech_text"] == original[0]["text"]
    assert json.loads(source.read_text(encoding="utf-8")) == original

    second = generate_dubbed_media(
        video, source, tmp_path / "work", model, quality=quality,
        speech_unit_overrides=saved, on_auto_rewrite=propose,
    )
    assert second.completed_keys == result.completed_keys
    assert len(model.calls) == 2
    assert proposals == []


def test_auto_rewrite_disabled_and_preview_never_changes_speech(tmp_path):
    video, source = _make_input(tmp_path)
    source.write_text(json.dumps([
        {"start": 0.2, "end": 0.7, "text": "我们也不是科学家，我懂。"},
    ], ensure_ascii=False), encoding="utf-8")
    proposals = []
    result = generate_dubbed_media(
        video, source, tmp_path / "disabled", TimedSynth(),
        quality={"auto_rewrite_speech": False},
        on_auto_rewrite=lambda *args: proposals.append(args) or "我懂。",
    )
    assert result.video_path.is_file()
    from videoroll.apps.dubbing.control import DubbingInterrupted
    with pytest.raises(DubbingInterrupted, match="preview_ready"):
        generate_dubbed_media(
            video, source, tmp_path / "preview", TimedSynth(),
            quality={"auto_rewrite_speech": True}, preview_only=True,
            on_auto_rewrite=lambda *args: proposals.append(args) or "我懂。",
        )
    assert proposals == []


def test_automatic_rewrite_gate_keeps_numbers_negations_and_technical_tokens():
    assert safe_automatic_rewrite("我们也不是科学家，我懂。", "我懂，我们也非专家。")
    assert not safe_automatic_rewrite("S100 电脑间距为 0.125 英寸。", "电脑间距缩小了。")
    assert not safe_automatic_rewrite("S100 电脑故障了。", "电脑故障。")
    assert not safe_automatic_rewrite("请不要启动机器。", "请启动机器。")
    assert not safe_automatic_rewrite("这是一段需要完整保留的中文技术说明文字。", "好。")


def test_group_fingerprint_invalidates_stale_rewrite_after_manual_change():
    raw = [
        {"start": 0.0, "end": 0.5, "text": "第一句，"},
        {"start": 0.5, "end": 1.2, "text": "继续说。"},
    ]
    units = build_speech_units(spoken_segments_from_json(raw, voice="Vivian"), enabled=True)
    assert len(units) == 1
    record = {"1": {"fingerprint": speech_fingerprint(units[0]), "speech_text": "更短的台词。"}}
    assert apply_speech_unit_overrides(units, record)[0].part.speech_text == "更短的台词。"
    changed = spoken_segments_from_json(raw, voice="Vivian", speech_overrides={"2": "现在改了。"})
    refreshed = build_speech_units(changed, enabled=True)
    assert apply_speech_unit_overrides(refreshed, record)[0].part.speech_text != "更短的台词。"


def test_job_persists_automatic_rewrite_and_editor_reports_real_audio(tmp_path, monkeypatch):
    from videoroll.apps.dubbing.job import execute_dubbing_job
    from videoroll.apps.orchestrator_api.routers.dubbing import list_dubbing_segments
    from videoroll.db.models import DubbingJob
    from videoroll.apps.dubbing import rewrite
    from videoroll.apps.subtitle_service import translate_settings_store
    from videoroll import config
    from tests.test_dubbing_production_controls import _prepare_job_media

    factory, store, task_id, job_id = _prepare_job_media(tmp_path, preview_only=False)
    raw = [
        {"start": 0.2, "end": 0.7, "text": "我们也不是科学家，我懂。"},
        {"start": 0.9, "end": 1.8, "text": "下一句话。"},
    ]
    store.path_for("sub/task/segments.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8",
    )
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        req = dict(job.request_json)
        req["quality"] = {"auto_rewrite_speech": True}
        req["cache_schema"] = 2
        req["model_path"] = "fake:duration-control"
        job.request_json = req
        db.commit()
    monkeypatch.setattr(rewrite, "propose_concise_script",
                        lambda **_kw: "我懂，我们也非专家。")
    monkeypatch.setattr(translate_settings_store, "get_translate_settings",
                        lambda *_a, **_kw: {})
    monkeypatch.setattr(config, "get_subtitle_settings", lambda: object())
    tts = TimedSynth()
    result = execute_dubbing_job(
        job_id, session_factory=factory, store=store,
        work_dir=tmp_path / "workspace",
        synthesizer_factory=lambda _model, _voice: tts,
    )
    assert "audio_key" in result
    assert len(tts.calls) == 2
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        assert job.status == "succeeded"
        assert not job.request_json.get("auto_speech_unit_overrides")
        listing = list_dubbing_segments(task_id, job_id, offset=0, limit=40, db=db, store=store)
        assert listing["items"][0]["subtitle_text"] == raw[0]["text"]
        assert not listing["items"][0]["auto_rewritten"]
        assert listing["items"][0]["speech_text"] == raw[0]["text"]
        assert listing["items"][0]["has_audio"]
