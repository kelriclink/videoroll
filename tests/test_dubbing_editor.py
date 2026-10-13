from __future__ import annotations

import json
import uuid
import wave
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from videoroll.apps.dubbing.pipeline import segment_cache_key, spoken_segments_from_json
from videoroll.apps.orchestrator_api.routers.dubbing import (
    DubbingSpeechUpdate,
    edit_dubbing_segment,
    list_dubbing_segments,
    preview_dubbing_segment,
)
from videoroll.db.models import Base, DubbingJob, SourceLicense, SourceType, Task, TaskStatus
from videoroll.storage.filesystem import FileStore


def _setup(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'editor.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    task_id, job_id = uuid.uuid4(), uuid.uuid4()
    store = FileStore(SimpleNamespace(storage_root=str(tmp_path / "storage")))
    store.ensure_ready()
    raw = [
        {"start": 1.0, "end": 2.5, "text": "字幕第一句"},
        {"start": 3.0, "end": 4.5, "text": "字幕第二句"},
    ]
    store.put_bytes(json.dumps(raw, ensure_ascii=False).encode(), "sub/task/final.json")
    with sessions() as db:
        db.add(Task(id=task_id, source_type=SourceType.local,
                    source_license=SourceLicense.own, status=TaskStatus.rendered))
        db.add(DubbingJob(
            id=job_id, task_id=task_id, status="failed",
            request_json={"source_segments_key": "sub/task/final.json", "voice": "Vivian",
                          "model_path": "mock-tts"},
            output_json={},
        ))
        db.commit()
    return sessions, store, task_id, job_id, raw


def test_editor_rewrites_only_spoken_text_and_keeps_subtitle(tmp_path):
    factory, store, task_id, job_id, raw = _setup(tmp_path)
    with factory() as db:
        original = list_dubbing_segments(task_id, job_id, offset=0, limit=40, db=db, store=store)
        assert original["total"] == 2
        assert original["items"][0]["subtitle_text"] == "字幕第一句"
        edited = edit_dubbing_segment(task_id, job_id, 1,
                                      DubbingSpeechUpdate(speech_text="缩短的口播"), db=db, store=store)
        assert edited["speech_text"] == "缩短的口播"
        page = list_dubbing_segments(task_id, job_id, offset=0, limit=40, db=db, store=store)
        assert page["items"][0]["speech_text"] == "缩短的口播"
        assert page["items"][0]["subtitle_text"] == "字幕第一句"
        assert page["items"][0]["overridden"] is True
    saved = json.loads(store.path_for("sub/task/final.json").read_text())
    assert saved == raw


def test_segment_preview_grants_only_current_hash(tmp_path):
    factory, store, task_id, job_id, raw = _setup(tmp_path)
    segment = spoken_segments_from_json(raw, voice="Vivian")[0]
    key = segment_cache_key(segment, "mock-tts")
    path = store.path_for(f"dub/{task_id}/tts/{key}.wav", require_exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * 240)
    with factory() as db:
        response = preview_dubbing_segment(task_id, job_id, 1, db=db, store=store)
        assert str(response.path) == str(path)
        edit_dubbing_segment(
            task_id, job_id, 1, DubbingSpeechUpdate(speech_text="新的口播内容"),
            db=db, store=store,
        )
        with pytest.raises(HTTPException) as error:
            preview_dubbing_segment(task_id, job_id, 1, db=db, store=store)
        assert error.value.status_code == 404


def test_editor_rejects_running_or_completed_jobs(tmp_path):
    factory, store, task_id, job_id, _ = _setup(tmp_path)
    with factory() as db:
        job = db.get(DubbingJob, job_id)
        job.status = "synthesizing"
        db.commit()
        with pytest.raises(HTTPException) as error:
            edit_dubbing_segment(
                task_id, job_id, 1, DubbingSpeechUpdate(speech_text="测试"),
                db=db, store=store,
            )
        assert error.value.status_code == 409
