"""Inspect and edit independent Chinese dubbing scripts without modifying subtitles."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from videoroll.apps.dubbing.pipeline import segment_cache_key, spoken_segments_from_json
from videoroll.apps.orchestrator_api.dependencies import get_db, get_store
from videoroll.db.models import DubbingJob
from videoroll.storage.filesystem import FileStore


router = APIRouter()


class DubbingSpeechUpdate(BaseModel):
    speech_text: str = Field(min_length=1, max_length=1000)


def _owned_job(db: Session, task_id: uuid.UUID, job_id: uuid.UUID, *, lock: bool = False) -> DubbingJob:
    query = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.id == job_id,
    )
    if lock:
        query = query.with_for_update()
    job = query.one_or_none()
    if job is None:
        raise HTTPException(404, "dubbing job not found")
    return job


def _speech_segments(job: DubbingJob, store: FileStore):
    source_key = str(job.request_json.get("source_segments_key") or "")
    try:
        raw = json.loads(store.path_for(source_key).read_text(encoding="utf-8"))
        return spoken_segments_from_json(
            raw,
            voice=str(job.request_json.get("voice") or "Vivian"),
            speech_overrides=job.request_json.get("speech_overrides") or {},
        )
    except (FileNotFoundError, ValueError, TypeError, KeyError, OSError) as exc:
        raise HTTPException(409, f"dubbing subtitle asset unavailable: {type(exc).__name__}") from exc


@router.get("/tasks/{task_id}/dubbing_jobs/{job_id}/segments")
def list_dubbing_segments(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=100),
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> dict:
    job = _owned_job(db, task_id, job_id)
    segments = _speech_segments(job, store)
    model = str(job.request_json.get("model_path") or "")
    successful = dict((job.output_json or {}).get("segments") or {})
    overrides = job.request_json.get("speech_overrides") or {}
    return {
        "total": len(segments), "offset": offset,
        "items": [
            {
                "index": item.index, "start": item.start, "end": item.end,
                "subtitle_text": item.subtitle_text, "speech_text": item.speech_text,
                "overridden": str(item.index) in overrides,
                "has_audio": segment_cache_key(item, model) in successful,
            }
            for item in segments[offset:offset + limit]
        ],
    }


@router.put("/tasks/{task_id}/dubbing_jobs/{job_id}/segments/{index}")
def edit_dubbing_segment(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    index: int,
    payload: DubbingSpeechUpdate,
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> dict:
    job = _owned_job(db, task_id, job_id, lock=True)
    if job.status not in {"queued", "failed"} or job.lease_owner:
        raise HTTPException(409, "only inactive queued/failed dubbing jobs can be edited")
    segments = _speech_segments(job, store)
    if index < 1 or index > len(segments):
        raise HTTPException(404, "dubbing segment not found")
    script = payload.speech_text.strip()
    if not script:
        raise HTTPException(422, "speech_text must not be empty")
    request = dict(job.request_json)
    overrides = dict(request.get("speech_overrides") or {})
    overrides[str(index)] = script
    request["speech_overrides"] = overrides
    job.request_json = request
    # The audio files remain cached under their content hashes, but progress
    # from the old spoken script must not be shown as the new script.
    job.output_json = {}
    job.progress = 0
    job.error_message = None
    db.commit()
    return {"index": index, "speech_text": script, "status": job.status}


@router.get("/tasks/{task_id}/dubbing_jobs/{job_id}/segments/{index}/audio")
def preview_dubbing_segment(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    index: int,
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> FileResponse:
    job = _owned_job(db, task_id, job_id)
    segments = _speech_segments(job, store)
    if index < 1 or index > len(segments):
        raise HTTPException(404, "dubbing segment not found")
    model = str(job.request_json.get("model_path") or "")
    key = segment_cache_key(segments[index - 1], model)
    saved = str((job.output_json or {}).get("segments", {}).get(key) or "")
    if not saved:
        # Previously generated clips remain independently cached. Only grant
        # access if they match this job's current voice, model and spoken text.
        saved = f"dub/{job.task_id}/tts/{key}.wav"
    try:
        path = store.path_for(saved)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "segment audio has not been generated") from exc
    return FileResponse(path, media_type="audio/wav",
                        headers={"Content-Disposition": f'inline; filename="segment-{index}.wav"'})
