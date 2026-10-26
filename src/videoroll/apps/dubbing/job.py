"""Native-only VideoRoll dubbing business interface.

VideoRoll owns task validation, leases and asset registration. The complete
media algorithm lives exclusively in upstream YouDub, not in this module.
Historical CosyVoice/Qwen jobs remain readable but cannot be executed.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from sqlalchemy.orm import Session, sessionmaker

from videoroll.db.models import Asset, AssetKind, DubbingJob, SubtitleJob, Task, TaskStatus
from videoroll.storage.filesystem import FileStore

LEASE_SECONDS = 1800
ACTIVE = frozenset(("queued", "preparing", "synthesizing", "aligning", "mixing", "rendering",
                    "pause_requested", "cancel_requested"))


class DubbingLeaseBusy(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lease_is_active(deadline: datetime | None) -> bool:
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline > _utcnow()


def create_dubbing_job(
    db: Session,
    *,
    task_id: uuid.UUID,
    subtitle_job_id: uuid.UUID,
    subtitle_video_key: str,
    operation_key: str,
    voice: str = "reference",
    model_path: str = "youdub:voxcpm2",
    **unused: Any,
) -> DubbingJob:
    # No legacy text-to-speech options or preview modes are accepted.
    if model_path != "youdub:voxcpm2" or voice != "reference":
        raise ValueError("Only YouDub VoxCPM2 reference-voice dubbing is supported")
    if any(unused.get(key) for key in (
        "preview_only", "auto_rewrite_speech", "auto_group_speech", "force_regenerate"
    )):
        raise ValueError("Legacy preview and speech rewriting are retired")
    prior = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.operation_key == operation_key,
    ).first()
    if prior is not None:
        return prior
    source_task = db.query(Task).filter(Task.id == task_id).with_for_update().one_or_none()
    subtitle_job = db.get(SubtitleJob, subtitle_job_id)
    if source_task is None or source_task.status == TaskStatus.canceled:
        raise ValueError("Video task not found or canceled")
    if subtitle_job is None or subtitle_job.task_id != task_id:
        raise ValueError("Chinese subtitle task not found")
    prior = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.operation_key == operation_key,
    ).first()
    if prior is not None:
        return prior
    competing = db.query(DubbingJob).filter(
        DubbingJob.task_id == task_id, DubbingJob.status.in_(ACTIVE),
    ).first()
    if competing:
        raise ValueError("Another dubbing task is already queued or running")
    if getattr(subtitle_job.status, "value", subtitle_job.status) != "succeeded":
        raise ValueError("Chinese subtitles must be completed")
    subtitle_key = str((subtitle_job.request_json or {}).get("artifacts", {}).get(
        "final_subtitle_segments_key") or "")
    if not subtitle_key:
        raise ValueError("Translated Chinese subtitle JSON is missing")
    source = db.query(Asset).filter(
        Asset.task_id == task_id, Asset.storage_key == subtitle_video_key,
        Asset.kind == AssetKind.video_raw,
    ).first()
    if source is None:
        raise ValueError("YouDub requires original source video (not burned-subtitle P1)")
    job = DubbingJob(
        id=uuid.uuid4(), task_id=task_id, subtitle_job_id=subtitle_job_id,
        operation_key=operation_key, status="queued", progress=0,
        request_json={
            "source_segments_key": subtitle_key,
            "subtitle_video_key": subtitle_video_key,
            "model_path": "youdub:voxcpm2",
            "voice": "reference",
            "preview_only": False,
            "cache_schema": "youdub-native-v1",
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
    synthesizer_factory: Any = None,  # old callers/tests cannot trigger legacy TTS
    **_unused: Any,
) -> dict[str, Any]:
    from videoroll.apps.dubbing.youdub_job import execute_native_job
    return execute_native_job(job_id, session_factory=session_factory,
                              store=store, work_dir=work_dir)
