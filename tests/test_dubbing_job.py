from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videoroll.apps.dubbing.job import create_dubbing_job, execute_dubbing_job, DubbingLeaseBusy
from videoroll.db.models import (
    Asset, AssetKind, Base, DubbingJob, SourceLicense, SourceType,
    SubtitleJob, SubtitleJobStatus, Task, TaskStatus,
)
from videoroll.storage.filesystem import FileStore


class FakeTTS:
    def __init__(self, model_path, voice):
        self.model_path = model_path
        self.voice = voice
        self.calls = []

    def synthesize(self, text, output, *, voice=None):
        self.calls.append(text)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=450:duration=0.28",
             "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(output)],
            check=True,
        )


@pytest.mark.parametrize("input_kind,task_status", [
    (AssetKind.video_final, TaskStatus.rendered),
    (AssetKind.video_raw, TaskStatus.published),
])
def test_dubbing_job_persists_two_artifacts_and_resumes(tmp_path, input_kind, task_status):
    db_file = tmp_path / "state.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{db_file}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = FileStore(SimpleNamespace(storage_root=str(tmp_path / "store")))
    store.ensure_ready()
    p1 = tmp_path / "p1.mp4"
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=160x90:d=3:r=25",
        "-f", "lavfi", "-i", "sine=frequency=100:duration=3",
        "-c:v", "mpeg4", "-c:a", "aac", "-t", "3", str(p1),
    ], check=True)
    p1_key = "render/task/p1.mp4" if input_kind == AssetKind.video_final else "raw/task/original.mp4"
    json_key = "sub/task/segments.json"
    store.upload_file(p1, p1_key)
    store.put_bytes(json.dumps([
        {"start": 0.5, "end": 1.2, "text": "中文第一句"},
        {"start": 1.7, "end": 2.4, "text": "中文第二句"},
    ], ensure_ascii=False).encode(), json_key)
    task_id, subtitle_id = uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.youtube, source_license=SourceLicense.own,
                    status=task_status))
        db.add(SubtitleJob(id=subtitle_id, task_id=task_id,
                           status=SubtitleJobStatus.succeeded,
                           request_json={"artifacts": {"final_subtitle_segments_key": json_key}}))
        db.add(Asset(task_id=task_id, kind=input_kind, storage_key=p1_key))
        db.commit()
        job = create_dubbing_job(db, task_id=task_id, subtitle_job_id=subtitle_id,
                                subtitle_video_key=p1_key, operation_key="p2:initial")
        assert job.status == "queued"
        same = create_dubbing_job(db, task_id=task_id, subtitle_job_id=subtitle_id,
                                  subtitle_video_key=p1_key, operation_key="p2:initial")
        assert same.id == job.id
        job_id = job.id

    engine_model = FakeTTS("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "Vivian")
    result = execute_dubbing_job(
        job_id, session_factory=factory, store=store,
        work_dir=tmp_path / "work", synthesizer_factory=lambda model, voice: engine_model,
    )
    assert len(engine_model.calls) == 2
    assert store.path_for(result["audio_key"]).is_file()
    assert store.path_for(result["video_key"]).is_file()
    assert result["source_video_key"] == p1_key
    assert result["subtitle_video_key"] == (p1_key if input_kind == AssetKind.video_final else None)
    with factory() as db:
        assert db.get(DubbingJob, job_id).status == "succeeded"
        assert db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.dubbing_audio).count() == 1
        assert db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_dubbed).count() == 1
    assert execute_dubbing_job(
        job_id, session_factory=factory, store=store,
        work_dir=tmp_path / "work", synthesizer_factory=lambda m, v: engine_model,
    )["video_key"] == result["video_key"]
    assert len(engine_model.calls) == 2


def test_manual_dubbing_can_start_after_subtitles_without_p1_even_if_published(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    task_id, subtitle_id = uuid.uuid4(), uuid.uuid4()
    raw_key = f"raw/{task_id}/original.webm"
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.youtube,
                    source_license=SourceLicense.own, status=TaskStatus.published))
        db.add(SubtitleJob(id=subtitle_id, task_id=task_id,
                           status=SubtitleJobStatus.succeeded,
                           request_json={"artifacts": {"final_subtitle_segments_key": f"sub/{task_id}/segments.json"}}))
        db.add(Asset(task_id=task_id, kind=AssetKind.video_raw, storage_key=raw_key))
        db.commit()
        job = create_dubbing_job(db, task_id=task_id, subtitle_job_id=subtitle_id,
                                 subtitle_video_key=raw_key, operation_key="manual:raw")
        assert job.status == "queued"
        assert job.request_json["source_video_kind"] == "video_raw"
        assert job.request_json["subtitle_video_key"] == raw_key
        assert db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_final).count() == 0


def test_missing_source_video_asset_cannot_be_queued(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    task_id, subtitle_id = uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add(Task(id=task_id, source_type=SourceType.local,
                    source_license=SourceLicense.own, status=TaskStatus.subtitle_ready))
        db.add(SubtitleJob(id=subtitle_id, task_id=task_id,
                           status=SubtitleJobStatus.succeeded,
                           request_json={"artifacts": {"final_subtitle_segments_key": "sub/final.json"}}))
        db.commit()
        with pytest.raises(ValueError, match="source raw or P1 video"):
            create_dubbing_job(db, task_id=task_id, subtitle_job_id=subtitle_id,
                               subtitle_video_key="render/not_ready.mp4", operation_key="p2:1")
