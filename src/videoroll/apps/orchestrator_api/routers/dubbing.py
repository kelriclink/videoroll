"""Inspect and edit independent Chinese dubbing scripts without modifying subtitles."""
from __future__ import annotations

import json
import uuid

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from videoroll.apps.dubbing.pipeline import segment_cache_key, spoken_segments_from_json
from videoroll.apps.dubbing.speech_units import (build_speech_units,
    apply_speech_unit_overrides)
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
    quality = (job.request_json.get("quality") or {}) if job.request_json.get("cache_schema") == 2 else None
    successful = dict((job.output_json or {}).get("segments") or {})
    prepared_segments = dict((job.output_json or {}).get("prepared_segments") or {})
    units = apply_speech_unit_overrides(build_speech_units(
        segments, enabled=bool((quality or {}).get('auto_group_speech', False))),
        job.request_json.get('auto_speech_unit_overrides'))
    membership = {index: (unit.part, unit.source_indices) for unit in units for index in unit.source_indices}
    overrides = job.request_json.get("speech_overrides") or {}
    return {
        "total": len(segments), "offset": offset,
        "items": [
            {
                "index": item.index, "start": item.start, "end": item.end,
                "subtitle_text": item.subtitle_text, "speech_text": item.speech_text,
                "overridden": str(item.index) in overrides,
                "auto_rewritten": (item.index == membership[item.index][0].index and
                    str(item.index) in (job.request_json.get('auto_speech_unit_overrides') or {}) and
                    membership[item.index][0].speech_text != item.speech_text),
                "has_audio": (item.index == membership[item.index][0].index and
                              segment_cache_key(membership[item.index][0],model,quality=quality) in successful),
                "prepared_audio": (prepared_segments.get(segment_cache_key(
                    membership[item.index][0], model, quality=quality))
                    if item.index == membership[item.index][0].index else None),
                "speech_unit_start": membership[item.index][0].index,
                "speech_unit_indices": list(membership[item.index][1]),
                "generated_speech_text": membership[item.index][0].speech_text if item.index == membership[item.index][0].index else None,
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
    if job.status not in {"queued", "failed", "paused", "needs_review", "preview_ready"} or job.lease_owner:
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
    request['speech_overrides'] = overrides
    # Manual edits supersede any automatic group rewrite containing this row.
    auto = dict(request.get('auto_speech_unit_overrides') or {})
    for leader, record in list(auto.items()):
        if isinstance(record, dict) and index in record.get('source_indices', []):
            del auto[leader]
    request['auto_speech_unit_overrides'] = auto
    job.request_json = request
    # The audio files remain cached under their content hashes, but progress
    # from the old spoken script must not be shown as the new script.
    # Keep successful cache references for other lines, but the edited line
    # now has a new content-addressed key. A preview must be regenerated if
    # its first line was edited.
    job.output_json = {**dict(job.output_json or {}), "timing_issue": None}
    if job.status in ("needs_review", "preview_ready"):
        job.status = "paused"
    job.error_message = None
    db.commit()
    return {"index": index, "speech_text": script, "status": job.status}


@router.post("/tasks/{task_id}/dubbing_jobs/{job_id}/segments/{index}/suggest")
def suggest_dubbing_speech(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    index: int,
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> dict:
    """Generate an optional proposal only; no writes to the task or audio."""
    from videoroll.apps.dubbing.rewrite import propose_concise_script
    from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
    from videoroll.config import get_subtitle_settings

    job = _owned_job(db, task_id, job_id)
    if job.status not in {"queued", "failed", "paused", "needs_review", "preview_ready"} or job.lease_owner:
        raise HTTPException(409, "pause the dubbing job before requesting a rewrite")
    items = _speech_segments(job, store)
    if not 1 <= index <= len(items):
        raise HTTPException(404, "segment not found")
    part = items[index - 1]
    next_start = items[index].start if index < len(items) else part.end + 0.5
    slot = max(0.1, min(part.end + 0.5, next_start - 0.08) - part.start)
    settings = get_translate_settings(db, get_subtitle_settings())
    # Do not keep a PostgreSQL transaction open across external inference.
    db.rollback()
    try:
        candidate = propose_concise_script(
            text=part.speech_text,
            target_seconds=slot,
            cfg=settings,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"rewrite model request failed: {type(exc).__name__}") from exc
    return {"index": index, "original": part.speech_text, "suggestion": candidate,
            "requires_approval": True, "target_seconds": slot}


@router.get("/tasks/{task_id}/dubbing_jobs/{job_id}/segments/{index}/audio")
def preview_dubbing_segment(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    index: int,
    original: bool = Query(default=False),
    db: Session = Depends(get_db),
    store: FileStore = Depends(get_store),
) -> FileResponse:
    job = _owned_job(db, task_id, job_id)
    segments = _speech_segments(job, store)
    if index < 1 or index > len(segments):
        raise HTTPException(404, "dubbing segment not found")
    model = str(job.request_json.get("model_path") or "")
    quality = (job.request_json.get("quality") or {}) if job.request_json.get("cache_schema") == 2 else None
    units = apply_speech_unit_overrides(build_speech_units(
        segments, enabled=bool((quality or {}).get('auto_group_speech', False))),
        job.request_json.get('auto_speech_unit_overrides'))
    leader=next((unit.part for unit in units if index in unit.source_indices),None)
    if leader is None:
        raise HTTPException(404,"speech unit not found")
    key = segment_cache_key(leader, model, quality=quality)
    output = job.output_json or {}
    saved = str(output.get("segments", {}).get(key) or "")
    prepared = (output.get("prepared_segments") or {}).get(key) or {}
    if saved and not original and prepared.get("storage_key"):
        saved = str(prepared["storage_key"])
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
