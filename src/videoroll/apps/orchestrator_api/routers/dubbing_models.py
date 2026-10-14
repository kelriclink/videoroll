"""Authenticated TTS settings, model inventory and explicit downloads."""
from __future__ import annotations

from typing import Literal
import uuid
from sqlalchemy.orm import Session
from fastapi import Depends
from videoroll.apps.orchestrator_api.dependencies import get_db, get_store
from videoroll.db.models import Asset, AssetKind, DubbingJob, SubtitleJob
from videoroll.storage.filesystem import FileStore
from videoroll.apps.dubbing.voice_profiles import list_profiles, create_profile, delete_profile, resolve
from videoroll.apps.dubbing.auto_reference import rank_reference_candidates, source_asr_for_job

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from videoroll.apps.dubbing.model_management import choose, download, inventory
from videoroll.apps.dubbing.worker_status import worker_status
from videoroll.apps.dubbing.service_client import unified_service_status
from videoroll.apps.dubbing.separation import available as demucs_available

router = APIRouter()


class DubbingSelection(BaseModel):
    model: Literal["Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "cosyvoice3:base", "cosyvoice3:rl"]
    voice: Literal["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "reference"]
    device: Literal["cuda:0", "cuda:1", "cpu"]


class ModelDownload(BaseModel):
    model: Literal["Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "cosyvoice3:base", "cosyvoice3:rl"]
    source: Literal["huggingface", "modelscope"] = "huggingface"


@router.get("/settings/dubbing")
def get_dubbing_config() -> dict:
    return {**inventory(), "voice_profiles": list_profiles(), "worker": worker_status(),
            "service": unified_service_status(),
            "separation": {"available": demucs_available(), "engine": "Demucs htdemucs"}}


@router.put("/settings/dubbing")
def save_dubbing_config(payload: DubbingSelection) -> dict:
    try:
        choose(payload.model, payload.voice, payload.device)
        return inventory()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/settings/dubbing/models/download", status_code=202)
def download_dubbing_model(payload: ModelDownload) -> dict:
    try:
        return download(payload.model, payload.source)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class VoiceReferenceFromVideo(BaseModel):
    task_id: uuid.UUID
    asset_key: str
    name: str
    start_seconds: float = 0.0
    duration_seconds: float = 10.0
    reference_text: str = ""
    authorization_confirmed: bool = False


@router.get("/settings/dubbing/voices")
def get_voice_profiles() -> list[dict]:
    return list_profiles()


@router.post("/settings/dubbing/voices", status_code=201)
def create_voice_profile(payload: VoiceReferenceFromVideo,
                         db: Session = Depends(get_db), store: FileStore = Depends(get_store)) -> dict:
    if not payload.authorization_confirmed:
        raise HTTPException(403, "voice cloning requires confirmation of authorization")
    asset = db.query(Asset).filter(Asset.task_id == payload.task_id,
                                   Asset.storage_key == payload.asset_key,
                                   Asset.kind.in_([AssetKind.video_raw, AssetKind.video_final])).one_or_none()
    if asset is None:
        raise HTTPException(404, "video asset not found for task")
    try:
        return create_profile(source=store.path_for(asset.storage_key), task_id=payload.task_id,
                              asset_key=asset.storage_key, name=payload.name,
                              start_seconds=payload.start_seconds,
                              duration_seconds=payload.duration_seconds,
                              reference_text=payload.reference_text,
                              consent=payload.authorization_confirmed)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/settings/dubbing/voices/{profile_id}")
def remove_voice_profile(profile_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    # A reference voice is pinned to a job. Never remove its bytes while the
    # worker or a user review is still in progress.
    jobs = db.query(DubbingJob).filter(DubbingJob.status.in_(
        ['queued','preparing','synthesizing','aligning','mixing',
         'pause_requested','cancel_requested','paused','preview_ready','needs_review'])).all()
    if any((job.request_json or {}).get('reference_profile_id') == str(profile_id) for job in jobs):
        raise HTTPException(409, "reference voice is used by an active dubbing job")
    try:
        delete_profile(str(profile_id))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {'deleted': str(profile_id)}


@router.get("/settings/dubbing/voices/{profile_id}/audio")
def preview_voice_reference(profile_id: uuid.UUID) -> FileResponse:
    try:
        _, path = resolve(str(profile_id))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="audio/wav", filename=f"reference-{profile_id}.wav")


class AutoVoiceReferenceRequest(BaseModel):
    subtitle_job_id: uuid.UUID
    candidate_index: int = 0
    authorization_confirmed: bool = False


def _auto_reference_choices(task_id: uuid.UUID, subtitle_job_id: uuid.UUID,
                            db: Session, store: FileStore) -> tuple[list[dict], Asset, str]:
    job = db.query(SubtitleJob).filter(
        SubtitleJob.id == subtitle_job_id, SubtitleJob.task_id == task_id,
    ).one_or_none()
    if job is None:
        raise HTTPException(404, 'subtitle job not found for this video')
    if getattr(job.status, 'value', job.status) != 'succeeded':
        raise HTTPException(409, 'complete the subtitle recognition task first')
    try:
        raw, asr_key = source_asr_for_job(db, store, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    options=rank_reference_candidates(raw,limit=3)
    if not options:
        raise HTTPException(409, 'original ASR has no suitable 3–16s continuous speech; use advanced manual selection')
    raw_videos=db.query(Asset).filter(
        Asset.task_id == task_id, Asset.kind == AssetKind.video_raw,
    ).all()
    request_input=(job.request_json or {}).get('input') or {}
    pinned_video=str(request_input.get('key') or '') if isinstance(request_input,dict) else ''
    source=next((a for a in raw_videos if a.storage_key==pinned_video), None)
    if source is None and len(raw_videos)==1:
        source=raw_videos[0]
    if source is None:
        raise HTTPException(409, 'cannot identify the original audio track for this subtitle job')
    try:
        if not store.path_for(source.storage_key).is_file():
            raise FileNotFoundError
    except (ValueError, FileNotFoundError, OSError):
        raise HTTPException(409, 'original source media has been deleted; automatic voice cloning is unavailable') from None
    return options,source,asr_key


@router.get('/tasks/{task_id}/dubbing/voice_candidates')
def suggest_auto_voice_references(task_id: uuid.UUID,
                                  subtitle_job_id: uuid.UUID = Query(),
                                  db: Session = Depends(get_db),
                                  store: FileStore = Depends(get_store)) -> dict:
    options,source,_ = _auto_reference_choices(task_id,subtitle_job_id,db,store)
    return {
        'candidates': options,
        'source_kind': source.kind.value,
        'selection_method': 'original_asr_contiguous_speech_v1',
        'notice': 'ASR does not guarantee one speaker or remove background music; review the extracted audio',
    }


@router.post('/tasks/{task_id}/dubbing/voice_profiles/auto', status_code=201)
def create_auto_voice_reference(task_id: uuid.UUID,
                                payload: AutoVoiceReferenceRequest,
                                db: Session = Depends(get_db),
                                store: FileStore = Depends(get_store)) -> dict:
    if not payload.authorization_confirmed:
        raise HTTPException(403,'voice cloning requires explicit authorization')
    options,source,asr_key = _auto_reference_choices(task_id,payload.subtitle_job_id,db,store)
    if not 0 <= payload.candidate_index < len(options):
        raise HTTPException(422,'invalid voice candidate index')
    chosen=options[payload.candidate_index]
    path=store.path_for(source.storage_key)
    # Do not keep a database transaction open during FFmpeg extraction.
    db.rollback()
    try:
        record=create_profile(source=path,task_id=task_id,asset_key=source.storage_key,
            name='视频原声 · 自动选取',
            start_seconds=chosen['start_seconds'],
            duration_seconds=chosen['duration_seconds'],
            reference_text=chosen['reference_text'],
            consent=True)
    except (ValueError,RuntimeError,OSError) as exc:
        raise HTTPException(409,str(exc)) from exc
    return {**record,'selection_method':'original_asr_contiguous_speech_v1',
            'candidate_index':payload.candidate_index,
            'source_segments_key':asr_key,
            'speech_coverage':chosen['speech_coverage']}
