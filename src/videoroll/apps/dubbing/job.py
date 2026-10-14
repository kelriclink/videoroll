"""Durable DubbingJob execution and per-fragment resumability.

No GPU model is imported until a queued job has acquired its own lease. DB
transactions are closed before network/FFmpeg/model calls, avoiding idle
PostgreSQL transactions during long synthesis. The caller dispatches this
function using the dedicated Hatchet dubbing pool.
"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.dubbing.pipeline import (
    SpeechSynthesizer, generate_dubbed_media, segment_cache_key, spoken_segments_from_json,
)
from videoroll.db.models import Asset, AssetKind, DubbingJob, SubtitleJob, Task, TaskStatus
from videoroll.apps.dubbing.quality import ACTIVE, normalize_quality
from videoroll.apps.dubbing.control import DubbingInterrupted, check_control
from videoroll.apps.dubbing.timing import SpeechTooLong
from videoroll.storage.filesystem import FileStore
from videoroll.utils.hashing import sha256_file


LEASE_SECONDS = 1800
logger = logging.getLogger(__name__)


class DubbingLeaseBusy(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lease_is_active(deadline: datetime | None) -> bool:
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        # SQLite test/dev sessions return naive DateTime even for timezone=True.
        # PostgreSQL uses aware timestamptz; keep UTC semantics consistent.
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline > _utcnow()


def create_dubbing_job(
    db: Session,
    *,
    task_id: uuid.UUID,
    subtitle_job_id: uuid.UUID,
    subtitle_video_key: str,
    operation_key: str,
    voice: str = "Vivian",
    model_path: str = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    retain_original_audio: bool = False,
    preview_only: bool = False,
    instruct: str = "",
    temperature: float = 0.9,
    top_p: float = 0.9,
    batch_size: int = 1,
    mix_mode: str = "replace",
    reference_profile_id: uuid.UUID | None = None,
    cosy_mode: str = "cross_lingual",
    cosy_speed: float = 1.0,
    debug_timing: bool = False,
    auto_group_speech: bool = False,
    auto_rewrite_speech: bool = False,
) -> DubbingJob:
    """Create a manual dubbing job from translated subtitles and raw or P1 video."""
    existing = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.operation_key == operation_key,
    ).first()
    if existing is not None:
        return existing
    # Serialize competing creates for a single source task even when callers
    # supply distinct idempotency keys. PostgreSQL locks the parent Task row.
    task = db.query(Task).filter(Task.id == task_id).with_for_update().one_or_none()
    subtitle_job = db.get(SubtitleJob, subtitle_job_id)
    # Recheck idempotency after the parent row lock to serialize competing
    # requests with the same operation key.
    existing = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.operation_key == operation_key,
    ).first()
    if existing is not None:
        return existing
    if task is None or subtitle_job is None or subtitle_job.task_id != task_id:
        raise ValueError("task or source subtitle job not found")
    if task.status == TaskStatus.canceled:
        raise ValueError("canceled task cannot be dubbed")
    competing = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.status.in_(list(ACTIVE)),
    ).first()
    if competing is not None:
        raise ValueError(f"another dubbing job is active: {competing.id}")
    from videoroll.apps.dubbing.model_management import COSY_MODELS, QWEN_MODELS
    profile_id, profile_sha = '', ''
    if model_path in COSY_MODELS:
        from videoroll.apps.dubbing.voice_profiles import resolve
        if voice != 'reference' or reference_profile_id is None:
            raise ValueError('select an authorized reference voice for CosyVoice3')
        profile, _ = resolve(str(reference_profile_id))
        if str(profile.get('task_id') or '') != str(task_id):
            raise ValueError('reference voice does not belong to this task')
        if cosy_mode == 'zero_shot' and not profile.get('reference_text'):
            raise ValueError('zero-shot mode requires the reference transcript')
        if cosy_mode == 'instruct' and not instruct.strip():
            raise ValueError('instruct mode requires an instruction')
        profile_id = str(reference_profile_id)
        profile_sha = str(profile['audio_sha256'])
    elif model_path in QWEN_MODELS:
        if voice not in ('Vivian', 'Serena', 'Uncle_Fu', 'Dylan', 'Eric'):
            raise ValueError('invalid Qwen3-TTS preset speaker')
    else:
        raise ValueError('unsupported TTS model')
    quality = normalize_quality(instruct=instruct, temperature=temperature,
                                top_p=top_p, batch_size=batch_size,
                                mix_mode=mix_mode, model=model_path,
                                cosy_speed=cosy_speed, cosy_mode=cosy_mode,
                                profile_id=profile_id, profile_sha256=profile_sha,
                                debug_timing=debug_timing,
                                auto_group_speech=auto_group_speech,
                                auto_rewrite_speech=auto_rewrite_speech)
    # Cache separates different installed model revisions even if the user
    # keeps the same user-facing model name.
    import hashlib
    from videoroll.apps.dubbing.model_management import folder
    marker = folder(model_path) / ".videoroll-complete.json"
    if marker.is_file():
        quality["_model_revision"] = hashlib.sha256(marker.read_bytes()).hexdigest()[:16]
    segments_key = str((subtitle_job.request_json or {}).get("artifacts", {}).get("final_subtitle_segments_key") or "").strip()
    if not segments_key or subtitle_job.status.value != "succeeded":
        raise ValueError("translated subtitles have not finished")
    video_asset = db.query(Asset).filter(
        Asset.task_id == task_id, Asset.storage_key == subtitle_video_key,
        Asset.kind.in_([AssetKind.video_raw, AssetKind.video_final]),
    ).first()
    if video_asset is None:
        raise ValueError("source raw or P1 video asset not found")
    # Preset Chinese voices are Qwen-only. CosyVoice3 uses its authorized
    # reference voice and must not be rejected by the Qwen speaker catalog.
    if model_path in QWEN_MODELS:
        from videoroll.apps.dubbing.qwen_tts import CHINESE_SPEAKERS
        if voice not in CHINESE_SPEAKERS:
            raise ValueError(f"unsupported TTS voice: {voice}")
    job = DubbingJob(
        id=uuid.uuid4(), task_id=task_id, subtitle_job_id=subtitle_job_id,
        operation_key=operation_key, status="queued",
        request_json={
            "source_segments_key": segments_key,
            "subtitle_video_key": subtitle_video_key,
            "source_video_kind": video_asset.kind.value,
            "voice": voice, "model_path": model_path,
            "retain_original_audio": bool(retain_original_audio),
            "preview_only": bool(preview_only),
            "reference_profile_id": profile_id or None,
            "cache_schema": 2,
            "quality": quality,
        },
        output_json={},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def execute_dubbing_job(
    job_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    store: FileStore,
    work_dir: Path,
    synthesizer_factory: Callable[[str, str], SpeechSynthesizer],
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    owner = f"dubbing:{uuid.uuid4().hex}"
    db = session_factory()
    try:
        job = db.query(DubbingJob).filter(DubbingJob.id == job_id).with_for_update().one_or_none()
        if job is None:
            raise ValueError("dubbing job not found")
        if job.status == "succeeded":
            return dict(job.output_json or {})
        if job.lease_owner and lease_is_active(job.lease_until):
            raise DubbingLeaseBusy(f"dubbing job {job_id} is already running")
        if job.status not in ("queued", "failed", "preparing", "synthesizing", "aligning", "mixing"):
            raise ValueError(f"dubbing job not runnable from {job.status}")
        task = db.get(Task, job.task_id)
        if task is None or task.status == TaskStatus.canceled:
            raise ValueError("source task is missing or canceled")
        request = dict(job.request_json or {})
        task_id = job.task_id
        job.lease_owner = owner
        job.lease_until = _utcnow() + timedelta(seconds=LEASE_SECONDS)
        job.heartbeat_at = _utcnow()
        job.status = "preparing"
        job.error_message = None
        db.commit()
    finally:
        db.close()

    workspace = work_dir / "dubbing" / str(job_id)
    try:
        video_key = str(request["subtitle_video_key"])
        segments_key = str(request["source_segments_key"])
        video = store.path_for(video_key)
        segments = store.path_for(segments_key)
        voice = str(request.get("voice") or "Vivian")
        quality = dict(request.get("quality") or {})
        cache_quality = quality if request.get("cache_schema") == 2 else None
        def safe_point() -> None:
            with session_factory() as control_db:
                check_control(control_db, job_id, owner)
        safe_point()
        model_path = str(request.get("model_path") or "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
        workspace.mkdir(parents=True, exist_ok=True)
        raw = json.loads(segments.read_text(encoding="utf-8"))
        speech_overrides = request.get('speech_overrides') or {}
        auto_overrides = request.get('auto_speech_unit_overrides') or {}
        spoken = spoken_segments_from_json(raw, voice=voice, speech_overrides=speech_overrides)
        from videoroll.apps.dubbing.speech_units import (build_speech_units,
            apply_speech_unit_overrides, speech_fingerprint)
        base_units = build_speech_units(spoken,
            enabled=bool(quality.get('auto_group_speech', False)))
        cache_spoken = [u.part for u in apply_speech_unit_overrides(base_units, auto_overrides)]
        cache_dir = workspace / "segments"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # A resumed task can run on another machine, provided FileStore uses
        # the same shared filesystem. Repopulate cache from persisted assets.
        for part in cache_spoken:
            key = segment_cache_key(part, model_path, quality=cache_quality)
            cached_key = f"dub/{task_id}/tts/{key}.wav"
            cached = store.path_for(cached_key, require_exists=False)
            local = cache_dir / f"{key}.wav"
            if cached.is_file() and not local.is_file():
                shutil.copyfile(cached, local)

        factory = synthesizer_factory(model_path, voice)
        def checkpoint(done: int, total: int, key: str, wav: Path) -> None:
            # Uploads and TTS run outside database transactions.
            uploaded_key = f"dub/{task_id}/tts/{key}.wav"
            stored = store.path_for(uploaded_key, require_exists=False)
            if not stored.is_file():
                store.upload_file(wav, uploaded_key, content_type="audio/wav")
            progress_db = session_factory()
            try:
                item = progress_db.get(DubbingJob, job_id)
                if item is None or item.lease_owner != owner:
                    raise DubbingInterrupted("lease_lost")
                if item.status == "pause_requested":
                    raise DubbingInterrupted("paused")
                if item.status == "cancel_requested":
                    raise DubbingInterrupted("canceled")
                task_row = progress_db.get(Task, task_id)
                if task_row is None or task_row.status == TaskStatus.canceled:
                    raise RuntimeError("source task stopped during dubbing")
                checkpoint_data = dict(item.output_json or {})
                completed = dict(checkpoint_data.get("segments") or {})
                completed[key] = uploaded_key
                checkpoint_data["segments"] = completed
                checkpoint_data["segments_total"] = total
                item.output_json = checkpoint_data
                item.progress = max(1, min(85, round(done / total * 85)))
                item.status = "synthesizing"
                item.heartbeat_at = _utcnow()
                item.lease_until = _utcnow() + timedelta(seconds=LEASE_SECONDS)
                progress_db.commit()
            finally:
                progress_db.close()
        def performance_update(count: int, elapsed: float, audio_seconds: float) -> None:
            with session_factory() as perf_db:
                item = perf_db.get(DubbingJob, job_id)
                if item is None or item.lease_owner != owner:
                    raise DubbingInterrupted("lease_lost")
                stats = dict((item.output_json or {}).get("performance") or {})
                stats["chunks"] = int(stats.get("chunks") or 0) + 1
                stats["wall_seconds"] = round(float(stats.get("wall_seconds") or 0) + elapsed, 3)
                stats["audio_seconds"] = round(float(stats.get("audio_seconds") or 0) + audio_seconds, 3)
                stats["last_batch_size"] = count
                item.output_json = {**dict(item.output_json or {}), "performance": stats}
                perf_db.commit()

        def stage_update(status: str, progress: int) -> None:
            stage_db = session_factory()
            try:
                item = stage_db.get(DubbingJob, job_id)
                task_row = stage_db.get(Task, task_id)
                if item is None or item.lease_owner != owner:
                    raise DubbingInterrupted("lease_lost")
                if item.status in ("pause_requested", "cancel_requested"):
                    raise DubbingInterrupted("paused" if item.status == "pause_requested" else "canceled")
                if task_row is None or task_row.status == TaskStatus.canceled:
                    raise RuntimeError("source task stopped during dubbing")
                item.status = status
                item.progress = max(item.progress, progress)
                item.heartbeat_at = _utcnow()
                item.lease_until = _utcnow() + timedelta(seconds=LEASE_SECONDS)
                stage_db.commit()
            finally:
                stage_db.close()

        def record_timing_adjustments(changes: list[dict]) -> None:
            with session_factory() as audit_db:
                item = audit_db.get(DubbingJob, job_id)
                if item is None or item.lease_owner != owner:
                    raise DubbingInterrupted("lease_lost")
                item.output_json = {
                    **dict(item.output_json or {}),
                    "source_timing_adjustments": changes,
                }
                audit_db.commit()

        def save_speech_units(groups: list[dict]) -> None:
            with session_factory() as status_db:
                item=status_db.get(DubbingJob,job_id)
                if item is None or item.lease_owner != owner:
                    raise DubbingInterrupted("lease_lost")
                item.output_json={**dict(item.output_json or {}),"speech_units":groups}
                status_db.commit()

        def automatic_rewrite(unit, target_seconds: float, audio_seconds: float) -> str | None:
            """Attempt one audited, guarded LLM rewrite without holding a DB transaction."""
            if (str(unit.part.index) in auto_overrides or
                    any(str(i) in speech_overrides for i in unit.source_indices)):
                return None  # A prior automatic attempt or human edit wins.
            safe_point()
            from videoroll.apps.dubbing.rewrite import (propose_concise_script,
                safe_automatic_rewrite)
            from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
            from videoroll.config import get_subtitle_settings
            try:
                with session_factory() as settings_db:
                    cfg = get_translate_settings(settings_db, get_subtitle_settings())
                    settings_db.rollback()
                candidate = propose_concise_script(
                    text=unit.part.speech_text, target_seconds=target_seconds, cfg=cfg)
            except (ValueError, RuntimeError, ImportError, OSError) as exc:
                logger.warning('automatic spoken rewrite unavailable for unit %d: %s',
                               unit.part.index, type(exc).__name__)
                return None
            except Exception as exc:
                # Network/API failures must not turn a recoverable timing issue
                # into a repeated worker crash. Control/lease errors still propagate.
                if isinstance(exc, DubbingInterrupted):
                    raise
                logger.warning('automatic spoken rewrite failed for unit %d: %s',
                               unit.part.index, type(exc).__name__)
                return None
            if not safe_automatic_rewrite(unit.part.speech_text, candidate):
                return None
            safe_point()
            with session_factory() as rewrite_db:
                item = rewrite_db.query(DubbingJob).filter(DubbingJob.id == job_id).with_for_update().one()
                if item.lease_owner != owner:
                    raise DubbingInterrupted('lease_lost')
                saved_request = dict(item.request_json or {})
                saved_overrides = dict(saved_request.get('auto_speech_unit_overrides') or {})
                if str(unit.part.index) in saved_overrides:
                    return None
                saved_overrides[str(unit.part.index)] = {
                    'fingerprint': speech_fingerprint(unit), 'speech_text': candidate,
                    'original_text': unit.part.speech_text, 'source_indices': list(unit.source_indices),
                    'target_seconds': round(target_seconds, 3),
                    'original_audio_seconds': round(audio_seconds, 3),
                }
                saved_request['auto_speech_unit_overrides'] = saved_overrides
                item.request_json = saved_request
                rewrite_db.commit()
            return candidate

        result = generate_dubbed_media(
            video, segments, workspace, factory,
            voice=voice, ffmpeg=ffmpeg, ffprobe=ffprobe,
            retain_original_audio=bool(request.get("retain_original_audio")),
            on_segment_done=checkpoint,
            speech_overrides=speech_overrides,
            speech_unit_overrides=auto_overrides,
            on_auto_rewrite=automatic_rewrite if quality.get('auto_rewrite_speech') else None,
            on_stage=stage_update,
            on_check=safe_point,
            quality=cache_quality,
            batch_size=int(quality.get("batch_size") or 1),
            on_performance=performance_update,
            on_timing_adjustment=record_timing_adjustments,
            on_speech_units=save_speech_units,
            preview_only=bool(request.get("preview_only")),
            mix_mode=str(quality.get("mix_mode") or (
                "original_low" if request.get("retain_original_audio") else "replace")),
        )
        track_key = f"dub/{task_id}/audio/{sha256_file(result.audio_path)}.wav"
        video_output_key = f"render/{task_id}/dubbed-{sha256_file(result.video_path)}.mp4"
        store.upload_file(result.audio_path, track_key, content_type="audio/wav")
        store.upload_file(result.video_path, video_output_key, content_type="video/mp4")
        final_db = session_factory()
        try:
            item = final_db.query(DubbingJob).filter(DubbingJob.id == job_id).with_for_update().one()
            if item.lease_owner != owner:
                raise DubbingInterrupted("lease_lost")
            if item.status in ("cancel_requested", "pause_requested"):
                raise DubbingInterrupted("canceled" if item.status == "cancel_requested" else "paused")
            task_row = final_db.get(Task, task_id)
            if task_row is None or task_row.status == TaskStatus.canceled:
                raise RuntimeError("source task stopped before dubbing finalization")
            for kind, key, path in (
                (AssetKind.dubbing_audio, track_key, result.audio_path),
                (AssetKind.video_dubbed, video_output_key, result.video_path),
            ):
                if final_db.query(Asset).filter(Asset.task_id == task_id, Asset.kind == kind, Asset.storage_key == key).first() is None:
                    final_db.add(Asset(task_id=task_id, kind=kind, storage_key=key,
                                       sha256=sha256_file(path), size_bytes=path.stat().st_size,
                                       duration_ms=round(result.source_duration * 1000)))
            successful_output = dict(item.output_json or {})
            successful_output.pop('timing_issue', None)
            item.output_json = {
                **successful_output,
                "audio_key": track_key, "video_key": video_output_key,
                "source_video_key": video_key,
                "subtitle_video_key": (video_key if request.get("source_video_kind", "video_final") == "video_final" else None),
                "duration": result.source_duration,
                "segments_total": result.segments_total,
            }
            item.progress = 100
            item.error_message = None
            item.status = "succeeded"
            item.lease_owner = None
            item.lease_until = None
            item.heartbeat_at = _utcnow()
            final_db.commit()
            return dict(item.output_json)
        finally:
            final_db.close()
    except (DubbingInterrupted, SpeechTooLong) as error:
        # Preview is a listening/voice-quality gate, not a final alignment
        # acceptance test. A successfully cached first WAV must be playable
        # even when it is too long for its target slot. Still flag the issue
        # and prevent final generation until the user shortens that script.
        result_status = error.status if isinstance(error, DubbingInterrupted) else "needs_review"
        with session_factory() as state_db:
            item = state_db.get(DubbingJob, job_id)
            if item is not None and item.lease_owner == owner:
                if isinstance(error, SpeechTooLong):
                    cached = dict((item.output_json or {}).get("segments") or {})
                    if bool(request.get("preview_only")) and error.index == 1 and cached:
                        result_status = "preview_ready"
                    item.status = result_status
                    item.error_message = str(error)[:2000]
                    item.output_json = {**dict(item.output_json or {}), "timing_issue": {
                        "segment_index": error.index, "duration": error.duration,
                        "slot": error.slot, "required_speed": error.required_speed,
                    }}
                elif error.status != "lease_lost":
                    item.status = error.status
                    item.error_message = None
                item.lease_owner = None
                item.lease_until = None
                state_db.commit()
        return {"status": result_status}
    except Exception as error:
        error_db = session_factory()
        try:
            item = error_db.get(DubbingJob, job_id)
            if item is not None and item.lease_owner == owner:
                item.error_message = f"{type(error).__name__}: {error}"[:2000]
                item.status = "failed"
                item.lease_owner = None
                item.lease_until = None
                error_db.commit()
        finally:
            error_db.close()
        raise
