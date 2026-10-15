"""Native YouDub orchestration tests: no legacy synthesis or alignment.

Only database and VideoRoll asset plumbing are mocked. Upstream stages are
independently exercised by the vendored native bridge tests.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videoroll.apps.dubbing import youdub_native, youdub_job
from videoroll.apps.dubbing.job import execute_dubbing_job
from videoroll.db.models import (
    Base, Task, TaskStatus, SourceType, SourceLicense,
    SubtitleJob, SubtitleJobStatus, Asset, AssetKind, DubbingJob,
)
from videoroll.storage.filesystem import FileStore


def _setup(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = FileStore(SimpleNamespace(storage_root=str(tmp_path / "objects")))
    store.ensure_ready()
    task_id, subtitle_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    video = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=s=160x90:d=2:r=20", "-c:v", "mpeg4",
                    str(video)], check=True)
    subtitles = tmp_path / "subtitles.json"
    subtitles.write_text(json.dumps([
        {"start": 0.12, "end": 1.60, "text": "火箭引擎是怎么工作的？"},
        {"start": 1.60, "end": 1.95, "text": "真有趣。"},
    ], ensure_ascii=False), encoding="utf-8")
    store.upload_file(video, "final/test.mp4")
    store.upload_file(subtitles, "sub/test.json")
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.local,
                    source_license=SourceLicense.own, status=TaskStatus.rendered))
        db.add(SubtitleJob(id=subtitle_id, task_id=task_id,
                           status=SubtitleJobStatus.succeeded,
                           request_json={"artifacts": {
                               "final_subtitle_segments_key": "sub/test.json"}}))
        db.add(Asset(task_id=task_id, kind=AssetKind.video_final,
                     storage_key="final/test.mp4"))
        db.add(DubbingJob(
            id=job_id, task_id=task_id, subtitle_job_id=subtitle_id,
            status="queued", progress=0,
            operation_key="native:youdub",
            request_json={"model_path": "youdub:voxcpm2", "voice": "reference",
                          "preview_only": False,
                          "subtitle_video_key": "final/test.mp4",
                          "source_segments_key": "sub/test.json",
                          "quality": {}},
            output_json={},
        ))
        db.commit()
    return factory, store, task_id, job_id, video


def test_srt_conversion_preserves_long_chinese_and_overlapping_end(tmp_path):
    raw = [
        {"start": 0.12, "end": 1.60, "text": "火箭引擎是怎么工作的？"},
        {"start": 1.40, "end": 2.88, "text": "下一句允许与字幕重叠"},
    ]
    count = youdub_native.convert_subtitle_json(raw, tmp_path / "native.srt")
    assert count == 2
    content = (tmp_path / "native.srt").read_text()
    assert "00:00:00,120 --> 00:00:01,600" in content
    assert "00:00:01,400 --> 00:00:02,880" in content
    assert "火箭引擎是怎么工作的？" in content
    with pytest.raises(ValueError, match="caption"):
        youdub_native.convert_subtitle_json([
            {"start": 10, "end": 8, "text": "无效"}
        ], tmp_path / "bad.srt")


def test_video_roll_job_uses_upstream_artifacts_and_sets_business_success(tmp_path, monkeypatch):
    factory, store, task_id, job_id, video = _setup(tmp_path)
    native_out = tmp_path / "native.mp4"
    native_audio = tmp_path / "native.wav"
    native_out.write_bytes(video.read_bytes())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=300:duration=2",
                    "-ar", "24000", "-ac", "1", str(native_audio)], check=True)
    calls = []

    def upstream(**kwargs):
        calls.append(kwargs["job_id"])
        kwargs["should_continue"]()
        output = kwargs["workspace"]
        output.mkdir(parents=True, exist_ok=True)
        youdub_native.convert_subtitle_json(
            json.loads(kwargs["raw_subtitles"].read_text()), output / "translated.srt"
        )
        kwargs["progress"]({
            "current_stage": "tts",
            "stages": [{"name": name,
                        "status": "succeeded" if name in ("download", "separate") else "running",
                        "progress": 30 if name == "tts" else 0}
                       for name in youdub_native.STAGES],
        })
        return youdub_native.NativeResult(
            video=native_out, audio=native_audio,
            stages=tuple({"name": name, "status": "succeeded", "progress": 100}
                         for name in youdub_native.STAGES),
        )

    monkeypatch.setattr(youdub_job, "run_native", upstream)
    result = execute_dubbing_job(
        job_id, session_factory=factory, store=store,
        work_dir=tmp_path / "work",
        synthesizer_factory=lambda *args: pytest.fail("Legacy TTS must never initialize"),
    )
    assert calls == [job_id]
    assert result["segments_total"] == 2
    assert result["youdub"]["stage"] == "done"
    assert store.path_for(result["video_key"]).is_file()
    assert store.path_for(result["audio_key"]).is_file()
    with factory() as db:
        j = db.get(DubbingJob, job_id)
        assert j.status == "succeeded" and j.progress == 100
        assert not j.lease_owner
        assert db.query(Asset).filter(Asset.kind == AssetKind.video_dubbed).count() == 1
        assert db.query(Asset).filter(Asset.kind == AssetKind.dubbing_audio).count() == 1
    # Re-executing a successful job cannot launch VoxCPM again.
    execute_dubbing_job(
        job_id, session_factory=factory, store=store,
        work_dir=tmp_path / "work",
        synthesizer_factory=lambda *_: pytest.fail("Legacy synth not allowed"),
    )
    assert calls == [job_id]


def test_native_failure_is_persisted_and_does_not_advertise_success(tmp_path, monkeypatch):
    factory, store, _task_id, job_id, _video = _setup(tmp_path)

    def failure(**kwargs):
        kwargs["progress"]({
            "current_stage": "separate",
            "stages": [{"name": "separate", "status": "running", "progress": 5}],
        })
        raise RuntimeError("upstream Demucs failed")

    monkeypatch.setattr(youdub_job, "run_native", failure)
    with pytest.raises(RuntimeError, match="upstream Demucs failed"):
        execute_dubbing_job(
            job_id, session_factory=factory, store=store,
            work_dir=tmp_path / "work",
            synthesizer_factory=lambda *_: pytest.fail("Legacy synth must not run"),
        )
    with factory() as db:
        j = db.get(DubbingJob, job_id)
        assert j.status == "failed"
        assert "upstream Demucs failed" in j.error_message
        assert not j.lease_owner
        assert j.progress < 100
        assert db.query(Asset).filter(Asset.kind == AssetKind.video_dubbed).count() == 0
