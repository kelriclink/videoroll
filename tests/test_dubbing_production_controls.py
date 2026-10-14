from __future__ import annotations
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videoroll.apps.dubbing.control import DubbingInterrupted, request_control, check_control
from videoroll.apps.dubbing.pipeline import generate_dubbed_media, segment_cache_key
from videoroll.apps.dubbing.quality import audio_cache_key, normalize_quality
from videoroll.apps.dubbing.separation import background_stem
from videoroll.apps.dubbing.timing import SpeechTooLong, SpokenSegment, check_segment_fit
from videoroll.db.models import Base, DubbingJob, SourceLicense, SourceType, Task, TaskStatus


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    task_id, job_id = uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.youtube, source_license=SourceLicense.own,
                    status=TaskStatus.rendered))
        db.add(DubbingJob(id=job_id, task_id=task_id, status="synthesizing",
                          lease_owner="owner", lease_until=datetime.now(timezone.utc) + timedelta(minutes=30),
                          request_json={}, output_json={"segments":{"one":"audio-cache.wav"}}))
        db.commit()
    return factory, task_id, job_id


@pytest.mark.parametrize("action,expected", [("pause", "paused"), ("cancel", "canceled")])
def test_active_job_control_is_cooperative_and_preserves_audio(action, expected):
    factory, task_id, job_id = _db()
    with factory() as db:
        j = request_control(db, task_id, job_id, action=action)
        assert j.status == f"{action}_requested"
        assert j.lease_owner == "owner"
        with pytest.raises(DubbingInterrupted) as exc:
            check_control(db, job_id, "owner")
        assert exc.value.status == expected
        assert j.output_json["segments"]["one"] == "audio-cache.wav"


def test_queued_cancel_is_terminal_without_starting_worker():
    factory, task_id, job_id = _db()
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        job.status, job.lease_owner = "queued", None
        db.commit()
        request_control(db, task_id, job_id, action="cancel")
        assert db.get(DubbingJob, job_id).status == "canceled"
        assert db.get(DubbingJob, job_id).lease_owner is None


def test_cache_invalidates_only_generation_changes():
    part = SpokenSegment(1, 0, 2, "字幕", "口播", "Vivian")
    q = normalize_quality(model="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
    a = segment_cache_key(part, "qwen1.7", quality=q)
    b = segment_cache_key(part, "qwen1.7", quality={**q, "temperature": 1.1})
    c = segment_cache_key(part, "qwen1.7", quality={**q, "batch_size": 4})
    d = segment_cache_key(part, "qwen1.7", quality={**q, "_model_revision": "2026-10-14"})
    assert a != b
    assert a == c
    assert a != d
    with pytest.raises(ValueError):
        normalize_quality(instruct="faster", model="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")


def test_overlong_speech_fails_first_segment_without_postprocessing():
    parts = [SpokenSegment(1, 1, 2, "短字幕", "长口播", "Vivian"),
             SpokenSegment(2, 3, 4, "短字幕", "中文", "Vivian")]
    with pytest.raises(SpeechTooLong) as exc:
        check_segment_fit(parts, 0, 6.0, video_duration=5)
    assert exc.value.index == 1
    assert exc.value.required_speed > 3


def _create_media(tmp: Path) -> tuple[Path, Path]:
    video = tmp / "source.mp4"
    subtitles = tmp / "segments.json"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=160x90:d=5:r=25",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=5",
                    "-c:v", "mpeg4", "-c:a", "aac", str(video)], check=True)
    subtitles.write_text(json.dumps([
        {"start": 0.5, "end": 1.5, "text": "第一句"},
        {"start": 2.5, "end": 3.5, "text": "第二句"},
    ], ensure_ascii=False))
    return video, subtitles


class RecordingTTS:
    model_path = "fake-model"
    def __init__(self, duration: float):
        self.duration = duration
        self.calls = 0
    def synthesize(self, text, output, *, voice=None):
        self.calls += 1
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency=300:duration={self.duration}",
                        "-ar", "24000", "-ac", "1", str(output)], check=True)


def test_overlong_first_clip_is_detected_before_generating_remaining_ones(tmp_path):
    video, subtitles = _create_media(tmp_path)
    tts = RecordingTTS(4.0)
    persisted = []
    with pytest.raises(SpeechTooLong) as exc:
        generate_dubbed_media(video, subtitles, tmp_path / "work", tts,
                             on_segment_done=lambda *args: persisted.append(args[0]))
    assert exc.value.index == 1
    assert persisted == [1]
    assert tts.calls == 1
    assert not (tmp_path / "work/dubbed.mp4").exists()


def test_first_sentence_preview_stops_early_and_can_reuse_cache(tmp_path):
    video, subtitles = _create_media(tmp_path)
    tts = RecordingTTS(0.3)
    persisted = []
    with pytest.raises(DubbingInterrupted) as exc:
        generate_dubbed_media(video, subtitles, tmp_path / "work", tts,
                             on_segment_done=lambda *args: persisted.append(args[0]),
                             preview_only=True)
    assert exc.value.status == "preview_ready"
    assert persisted == [1]
    assert tts.calls == 1
    generated = generate_dubbed_media(video, subtitles, tmp_path / "work", tts)
    assert generated.video_path.exists()
    assert tts.calls == 2  # only second sentence re-generated


def test_demucs_integration_refuses_to_claim_availability_without_model(tmp_path):
    with patch("videoroll.apps.dubbing.separation.available", return_value=False):
        with pytest.raises(RuntimeError, match="not installed"):
            background_stem(tmp_path / "source.mp4", tmp_path / "work")


def _prepare_job_media(tmp_path: Path, *, preview_only: bool = True):
    """Persist a production-like task and source assets in isolated SQLite."""
    from types import SimpleNamespace
    from videoroll.db.models import Asset, AssetKind, SubtitleJob, SubtitleJobStatus
    from videoroll.apps.dubbing.job import create_dubbing_job
    from videoroll.storage.filesystem import FileStore
    file = tmp_path / "app.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{file}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = FileStore(SimpleNamespace(storage_root=str(tmp_path / "store")))
    store.ensure_ready()
    video, segments = _create_media(tmp_path)
    store.upload_file(video, "raw/task/video.mp4")
    store.upload_file(segments, "sub/task/segments.json")
    task_id, subtitle_id = uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.youtube,
                    source_license=SourceLicense.own, status=TaskStatus.rendered))
        db.add(SubtitleJob(id=subtitle_id, task_id=task_id,
                           status=SubtitleJobStatus.succeeded,
                           request_json={"artifacts": {"final_subtitle_segments_key": "sub/task/segments.json"}}))
        db.add(Asset(task_id=task_id, kind=AssetKind.video_raw, storage_key="raw/task/video.mp4"))
        db.commit()
        job = create_dubbing_job(db, task_id=task_id, subtitle_job_id=subtitle_id,
                                 subtitle_video_key="raw/task/video.mp4", operation_key="manual:1",
                                 preview_only=preview_only)
    return factory, store, task_id, job.id


def test_preview_checkpoint_and_confirmed_resume_reuses_first_segment(tmp_path):
    from videoroll.apps.dubbing.job import execute_dubbing_job
    factory, store, task_id, job_id = _prepare_job_media(tmp_path)
    tts = RecordingTTS(0.25)
    first = execute_dubbing_job(job_id, session_factory=factory, store=store,
                                work_dir=tmp_path / "scratch",
                                synthesizer_factory=lambda model, voice: tts)
    assert first == {"status": "preview_ready"}
    with factory() as db:
        j = db.get(DubbingJob, job_id)
        assert j.status == "preview_ready"
        assert j.lease_owner is None
        assert len(j.output_json["segments"]) == 1
        assert j.output_json["segments_total"] == 2
        with pytest.raises(ValueError, match="active"):
            # The preview is the only active job for that video.
            from videoroll.apps.dubbing.job import create_dubbing_job
            create_dubbing_job(db, task_id=task_id, subtitle_job_id=j.subtitle_job_id,
                               subtitle_video_key="raw/task/video.mp4", operation_key="manual:duplicate")
        db.rollback()
        request = dict(j.request_json)
        request["preview_only"] = False
        j.request_json = request
        j.status = "queued"
        db.commit()
    result = execute_dubbing_job(job_id, session_factory=factory, store=store,
                                 work_dir=tmp_path / "scratch",
                                 synthesizer_factory=lambda model, voice: tts)
    assert tts.calls == 2
    assert "audio_key" in result and "video_key" in result
    with factory() as db:
        assert db.get(DubbingJob, job_id).status == "succeeded"


def test_cancel_request_during_one_sentence_stops_before_next_sentence(tmp_path):
    from videoroll.apps.dubbing.job import execute_dubbing_job
    factory, store, task_id, job_id = _prepare_job_media(tmp_path, preview_only=False)

    class CancelAfterFirst(RecordingTTS):
        def synthesize(self, text, output, *, voice=None):
            super().synthesize(text, output, voice=voice)
            if self.calls == 1:
                with factory() as db:
                    state = request_control(db, task_id, job_id, action="cancel")
                    assert state.status == "cancel_requested"

    tts = CancelAfterFirst(0.25)
    result = execute_dubbing_job(job_id, session_factory=factory, store=store,
                                 work_dir=tmp_path / "scratch",
                                 synthesizer_factory=lambda model, voice: tts)
    assert result == {"status": "canceled"}
    assert tts.calls == 1
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        assert job.status == "canceled"
        assert job.lease_owner is None
        assert db.get(Task, task_id).status == TaskStatus.rendered


def test_batch_path_is_real_and_writes_independent_checkpoints(tmp_path):
    video, segments = _create_media(tmp_path)

    class Batched(RecordingTTS):
        def __init__(self):
            super().__init__(0.25)
            self.batch_sizes = []

        def synthesize_batch(self, texts, outputs, *, voices):
            self.batch_sizes.append(len(texts))
            for text, path, voice in zip(texts, outputs, voices):
                super().synthesize(text, path, voice=voice)

    tts = Batched()
    recorded = []
    result = generate_dubbed_media(video, segments, tmp_path / "work", tts,
                                   batch_size=2, on_segment_done=lambda n, *a: recorded.append(n))
    assert result.video_path.exists()
    assert recorded == [1, 2]
    assert tts.batch_sizes == [2]


def test_demucs_background_mode_can_pass_stem_into_ffmpeg(tmp_path, monkeypatch):
    """No actual Demucs model is downloaded; validate invocation and output layout."""
    import wave
    from videoroll.apps.dubbing import separation
    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if "demucs" in command[0]:
            wav_input = Path(command[-1])
            target = tmp_path / "work" / "separated" / "htdemucs" / wav_input.stem / "no_vocals.wav"
        else:
            target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 44100)
    monkeypatch.setattr(separation, "available", lambda: True)
    monkeypatch.setattr(separation.subprocess, "run", fake_run)
    output = background_stem(src, tmp_path / "work")
    assert output.is_file()
    assert "no_vocals.wav" == output.name
    assert len(calls) == 2
    assert "--two-stems=vocals" in calls[1]


def test_mixing_with_demucs_stem_is_explicit():
    from videoroll.apps.dubbing.mux import build_dubbed_part_command
    cmd = build_dubbed_part_command(
        "ffmpeg", Path("original.mp4"), Path("spoken.wav"), Path("dub.mp4"),
        video_duration=8, original_audio_present=True,
        background_track=Path("no_vocals.wav"),
    )
    assert "no_vocals.wav" in cmd
    assert "volume=0.65" in cmd[cmd.index("-filter_complex") + 1]
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"


def test_llm_rewrite_is_proposal_only_and_never_overwrites_original(monkeypatch):
    from videoroll.apps.dubbing.rewrite import propose_concise_script
    from videoroll.apps.dubbing import rewrite
    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "用中文说明关键技术。"}}]}
    calls = []
    def fake_post(url, *, headers, json, timeout, follow_redirects):
        calls.append((url, json))
        return FakeResponse()
    monkeypatch.setattr(rewrite.httpx, "post", fake_post)
    original = "请用中文仔细解释清楚我们所说到的这个关键技术。"
    candidate = propose_concise_script(
        text=original, target_seconds=3.0,
        cfg={"openai_base_url": "https://translate.example/v1",
             "openai_model": "local-model", "openai_api_key": "test-token"},
    )
    assert candidate == "用中文说明关键技术。"
    assert original == "请用中文仔细解释清楚我们所说到的这个关键技术。"
    assert calls[0][0].endswith("/chat/completions")
    assert calls[0][1]["temperature"] == 0.2


def test_llm_rewrite_refuses_missing_configuration_or_unshortened_result(monkeypatch):
    from videoroll.apps.dubbing import rewrite
    with pytest.raises(RuntimeError):
        rewrite.propose_concise_script(text="这是很长的中文口播稿", target_seconds=2.0,
                                       cfg={"openai_model": "local"})
    class Response:
        def raise_for_status(self):pass
        def json(self):return {"choices":[{"message":{"content":"这是很长的中文口播稿以及不该增加的文字"}}]}
    monkeypatch.setattr(rewrite.httpx, "post", lambda *a, **k: Response())
    with pytest.raises(ValueError, match="shorter safe"):
        rewrite.propose_concise_script(text="这是很长的中文口播稿", target_seconds=2.0,
                                       cfg={"openai_model": "local", "openai_api_key": "k",
                                            "openai_base_url": "https://translate.example/v1"})


def test_preview_passes_when_last_subtitle_extends_past_video_end(tmp_path):
    video, subtitles = _create_media(tmp_path)
    source = json.loads(subtitles.read_text(encoding='utf-8'))
    source[-1]['end'] = 7.1  # Deliberately beyond the local video duration.
    subtitles.write_text(json.dumps(source,ensure_ascii=False),encoding='utf-8')
    tts=RecordingTTS(0.3)
    fixes=[]
    with pytest.raises(DubbingInterrupted) as err:
        generate_dubbed_media(video,subtitles,tmp_path/'work',tts,
            preview_only=True,on_timing_adjustment=fixes.extend)
    assert err.value.status=='preview_ready'
    assert tts.calls==1
    assert fixes and fixes[0]['segment_index']==len(source)
    assert fixes[0]['original_end']==7.1
    assert json.loads(subtitles.read_text(encoding='utf-8'))[-1]['end']==7.1


def test_overlong_first_preview_keeps_audio_and_requires_edit_before_full_generation(tmp_path):
    from videoroll.apps.dubbing.job import execute_dubbing_job
    factory, store, _, job_id = _prepare_job_media(tmp_path)
    # Valid TTS sound with a 5s duration cannot fit the first subtitle slot.
    tts = RecordingTTS(5.0)
    outcome = execute_dubbing_job(job_id,session_factory=factory,store=store,
        work_dir=tmp_path/'scratch',synthesizer_factory=lambda model,voice:tts)
    assert outcome == {'status':'preview_ready'}
    with factory() as db:
        row=db.get(DubbingJob,job_id)
        assert row.status=='preview_ready'
        assert row.request_json['preview_only'] is True
        assert row.output_json['timing_issue']['segment_index']==1
        assert row.output_json['timing_issue']['required_speed'] > 1.25
        assert len(row.output_json['segments'])==1
        wav=store.path_for(next(iter(row.output_json['segments'].values())))
        assert wav.is_file() and wav.stat().st_size>44
    assert tts.calls==1


def test_preview_with_timing_issue_cannot_start_full_job_from_api(tmp_path):
    from fastapi import HTTPException
    from videoroll.apps.orchestrator_api.routers.tasks import confirm_dubbing_preview
    from videoroll.db.models import DubbingJob
    factory, _, task_id, job_id = _prepare_job_media(tmp_path)
    with factory() as db:
        row=db.get(DubbingJob,job_id)
        row.status='preview_ready'
        row.output_json={'timing_issue':{'segment_index':1,'required_speed':1.41},
                         'segments':{'sample':'stored.wav'}}
        db.commit()
    with patch('videoroll.apps.dubbing.worker_status.worker_status',return_value={'available':True}):
        with factory() as db, pytest.raises(HTTPException) as caught:
            confirm_dubbing_preview(task_id,job_id,settings=None,db=db)
        assert caught.value.status_code == 409
    with factory() as db:
        job=db.get(DubbingJob,job_id)
        assert job.status=='preview_ready'
        assert job.request_json['preview_only'] is True
