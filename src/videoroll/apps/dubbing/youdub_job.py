"""Durable VideoRoll job runner delegating all media processing to native YouDub.

This adapter only handles business DB leases, progress, artifact registration.
The original YouDub PipelineRunner owns separation, VoxCPM2, alignment, mixing,
caption timing and MP4 encoding.
"""
from __future__ import annotations

import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.dubbing.control import DubbingInterrupted, check_control
from videoroll.apps.dubbing.youdub_native import run_native, STAGES, convert_subtitle_json
from videoroll.db.models import Asset, AssetKind, DubbingJob, RenderJob, RenderJobStatus, Task, TaskStatus
from videoroll.storage.filesystem import FileStore
from videoroll.utils.hashing import sha256_file


def _duration(video: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        check=True, capture_output=True, text=True, timeout=45,
    )
    duration = float(result.stdout.strip())
    if not 0 < duration < 86400:
        raise ValueError("Invalid YouDub output duration")
    return duration


def execute_native_job(
    job_id: uuid.UUID, *, session_factory: sessionmaker[Session],
    store: FileStore, work_dir: Path,
) -> dict[str, Any]:
    # Retain VideoRoll's lease, single-job protection and durable Asset model.
    from videoroll.apps.dubbing.job import (
        DubbingLeaseBusy, LEASE_SECONDS, _utcnow, lease_is_active,
    )
    owner = f"youdub:{uuid.uuid4().hex}"
    with session_factory() as db:
        job = db.query(DubbingJob).filter(DubbingJob.id == job_id).with_for_update().one()
        if job.status == "succeeded":
            return dict(job.output_json or {})
        if job.status == "rendering":
            return {"status": "rendering", **dict(job.output_json or {})}
        if job.lease_owner and lease_is_active(job.lease_until):
            raise DubbingLeaseBusy(f"Native YouDub job {job_id} already leased")
        if job.status not in ("queued", "failed", "preparing", "synthesizing", "aligning", "mixing"):
            raise ValueError(f"Cannot run native job in state {job.status}")
        task = db.get(Task, job.task_id)
        if task is None or task.status == TaskStatus.canceled:
            raise ValueError("Source task canceled or missing")
        request = dict(job.request_json or {})
        if request.get("preview_only"):
            raise ValueError("Native YouDub only supports complete pipeline tasks")
        if request.get("model_path") != "youdub:voxcpm2":
            raise ValueError("Not a native YouDub dubbing job")
        task_id = job.task_id
        job.lease_owner = owner
        job.lease_until = _utcnow() + timedelta(seconds=LEASE_SECONDS)
        job.heartbeat_at = _utcnow()
        job.status = "preparing"
        job.error_message = None
        db.commit()

    workspace = work_dir / "dubbing" / str(job_id)
    try:
        video = store.path_for(str(request["subtitle_video_key"]))
        segments = store.path_for(str(request["source_segments_key"]))

        def control() -> None:
            with session_factory() as db:
                check_control(db, job_id, owner)

        def update(upstream: dict) -> None:
            stage_name = str(upstream.get("current_stage") or "download")
            stages = upstream.get("stages") or []
            complete = sum(x.get("status") in ("succeeded", "skipped") for x in stages)
            partial = next((x.get("progress") or 0 for x in stages
                            if x.get("name") == stage_name), 0)
            progress = max(1, min(96, int((complete + partial / 100) * 96 / len(STAGES))))
            state = ("synthesizing" if stage_name == "tts" else
                     "aligning" if stage_name in ("merge_audio", "split_audio") else
                     "mixing" if stage_name == "merge_video" else "preparing")
            with session_factory() as db:
                item = db.query(DubbingJob).filter(DubbingJob.id == job_id).with_for_update().one_or_none()
                if item is None or item.lease_owner != owner:
                    raise DubbingInterrupted("lease_lost")
                if item.status in ("pause_requested", "cancel_requested"):
                    raise DubbingInterrupted(
                        "paused" if item.status == "pause_requested" else "canceled"
                    )
                item.status = state
                item.progress = max(item.progress, progress)
                item.lease_until = _utcnow() + timedelta(seconds=LEASE_SECONDS)
                item.heartbeat_at = _utcnow()
                record = dict(item.output_json or {})
                record["youdub"] = {
                    "task_id": str(job_id), "stage": stage_name, "stages": stages,
                }
                item.output_json = record
                db.commit()

        result = run_native(
            job_id=job_id, video=video, raw_subtitles=segments,
            workspace=workspace, progress=update, should_continue=control,
        )
        control()
        if result.mixed_audio is not None:
            # Durable handoff: YouDub has finished *audio* only. The parent
            # workflow must wait for Render Worker to create video_dubbed.
            if result.aligned_srt is None:
                raise RuntimeError("Audio-only YouDub did not provide aligned subtitles")
            duration = _duration(result.mixed_audio)
            voice_key = f"dub/{task_id}/audio/voice-{sha256_file(result.audio)}.wav"
            mixed_key = f"dub/{task_id}/audio/mixed-{sha256_file(result.mixed_audio)}.m4a"
            srt_key = f"sub/{task_id}/dubbed-{sha256_file(result.aligned_srt)}.srt"
            store.upload_file(result.audio, voice_key, content_type="audio/wav")
            store.upload_file(result.mixed_audio, mixed_key, content_type="audio/mp4")
            store.upload_file(result.aligned_srt, srt_key, content_type="application/x-subrip")
            control()
            with session_factory() as db:
                from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
                job = db.query(DubbingJob).filter(DubbingJob.id == job_id).with_for_update().one()
                if job.lease_owner != owner or job.status in ("pause_requested", "cancel_requested"):
                    raise DubbingInterrupted("lease_lost")
                op_key = f"youdub:p2-render:{job_id}"
                render = db.query(RenderJob).filter(RenderJob.operation_key == op_key).first()
                if render is None:
                    profile = dict(get_auto_profile(db))
                    render = RenderJob(
                        task_id=task_id,
                        subtitle_job_id=None,
                        status=RenderJobStatus.queued,
                        operation_key=op_key,
                        request_json={
                            "input_key": request["subtitle_video_key"],
                            "srt_key": srt_key,
                            "audio_key": mixed_key,
                            "burn_in": True,
                            "runtime_profile": False,
                            "dubbing_job_id": str(job_id),
                            "render": {
                                "video_codec": profile.get("video_codec") or "h264",
                                "video_preset": profile.get("video_preset") or "fast",
                                "video_crf": profile.get("video_crf"),
                                "ass_style": profile.get("ass_style") or "clean_white",
                                "primary_font_scale_percent": profile.get("primary_font_scale_percent") or 100,
                                "secondary_font_scale_percent": profile.get("secondary_font_scale_percent") or 100,
                            },
                        },
                    )
                    db.add(render)
                    db.flush()
                elif render.status in {RenderJobStatus.failed, RenderJobStatus.canceled}:
                    # Retry ONLY the video stage; upstream VoxCPM2 audio assets
                    # are already complete and should not be synthesized again.
                    render.status = RenderJobStatus.queued
                    render.progress = 0
                    render.retry_count = 0
                    render.error_message = None
                    render.started_at = render.finished_at = None
                    render.lease_owner = render.lease_until = None
                    db.add(render)
                for key, path in ((mixed_key, result.mixed_audio), (voice_key, result.audio)):
                    if db.query(Asset).filter(Asset.task_id == task_id,
                            Asset.kind == AssetKind.dubbing_audio, Asset.storage_key == key).first() is None:
                        db.add(Asset(task_id=task_id, kind=AssetKind.dubbing_audio,
                                     storage_key=key, sha256=sha256_file(path),
                                     size_bytes=path.stat().st_size, duration_ms=round(duration * 1000)))
                output = dict(job.output_json or {})
                output.update({
                    "audio_key": mixed_key, "voice_key": voice_key,
                    "aligned_srt_key": srt_key,
                    "source_video_key": request["subtitle_video_key"],
                    "render_job_id": str(render.id),
                    "duration": duration,
                    "youdub": {"task_id": str(job_id), "stage": "rendering_p2",
                               "stages": list(result.stages)},
                })
                job.output_json = output
                job.status = "rendering"
                job.progress = max(job.progress, 96)
                job.lease_owner = job.lease_until = None
                job.error_message = None
                job.heartbeat_at = _utcnow()
                db.commit()
                return {"status": "rendering", **output}
        duration = _duration(result.video)
        native_srt = workspace / "translated.srt"
        # Count actual translated cues, not the 9 YouDub stages.
        actual_segments = sum(1 for line in native_srt.read_text(encoding="utf-8").splitlines()
                              if " --> " in line)
        track_key = f"dub/{task_id}/audio/{sha256_file(result.audio)}.wav"
        video_key = f"render/{task_id}/dubbed-{sha256_file(result.video)}.mp4"
        store.upload_file(result.audio, track_key, content_type="audio/wav")
        store.upload_file(result.video, video_key, content_type="video/mp4")
        control()
        with session_factory() as db:
            job = db.query(DubbingJob).filter(DubbingJob.id == job_id).with_for_update().one()
            if job.lease_owner != owner:
                raise DubbingInterrupted("lease_lost")
            if job.status in ("pause_requested", "cancel_requested"):
                raise DubbingInterrupted("paused" if job.status == "pause_requested" else "canceled")
            for kind, key, path in (
                (AssetKind.dubbing_audio, track_key, result.audio),
                (AssetKind.video_dubbed, video_key, result.video),
            ):
                existing = db.query(Asset).filter(
                    Asset.task_id == task_id, Asset.kind == kind,
                    Asset.storage_key == key,
                ).first()
                if existing is None:
                    db.add(Asset(
                        task_id=task_id, kind=kind, storage_key=key,
                        sha256=sha256_file(path), size_bytes=path.stat().st_size,
                        duration_ms=round(duration * 1000),
                    ))
            output = dict(job.output_json or {})
            output.update({
                "audio_key": track_key,
                "video_key": video_key,
                "source_video_key": request["subtitle_video_key"],
                "duration": duration,
                "segments_total": actual_segments,
                "youdub": {"task_id": str(job_id), "stage": "done",
                           "stages": list(result.stages)},
            })
            output.pop("timing_issue", None)
            job.output_json = output
            job.status = "succeeded"
            job.progress = 100
            job.lease_owner = job.lease_until = None
            job.error_message = None
            job.heartbeat_at = _utcnow()
            db.commit()
            return dict(output)
    except BaseException as error:
        with session_factory() as db:
            job = db.get(DubbingJob, job_id)
            if job is not None and job.lease_owner == owner:
                job.status = (
                    error.status if isinstance(error, DubbingInterrupted) else "failed"
                )
                job.error_message = (
                    None if isinstance(error, DubbingInterrupted)
                    else f"{type(error).__name__}: {error}"[:2000]
                )
                job.lease_owner = job.lease_until = None
                db.commit()
        if isinstance(error, DubbingInterrupted):
            return {"status": error.status}
        raise
