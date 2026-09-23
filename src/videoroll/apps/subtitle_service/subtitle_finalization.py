from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import uuid

from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.processing import (
    Segment,
    probe_video_resolution,
    segments_to_ass,
    segments_to_json_data,
    segments_to_srt,
    write_json,
)
from videoroll.db.models import (
    Asset,
    AssetKind,
    RenderJob,
    RenderJobStatus,
    Subtitle,
    SubtitleFormat,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.storage.filesystem import FileStore
from videoroll.utils.hashing import sha256_file


UniqueStorageKey = Callable[[str, str, str], str]
LogLine = Callable[[str], None]
NoArgCallback = Callable[[], None]
EnsureNotStopped = Callable[[], None]


@dataclass(slots=True)
class SubtitleOutputArtifacts:
    final_segments_key: str
    srt_key: str
    ass_key: str | None


_RESUME_READY_STATUSES = {
    TaskStatus.failed,
    TaskStatus.created,
    TaskStatus.ingested,
    TaskStatus.downloaded,
    TaskStatus.audio_extracted,
    TaskStatus.asr_done,
    TaskStatus.translated,
}


def build_render_job_payload(
    *,
    request_json: dict[str, Any],
    input_key: str,
    srt_key: str,
    ass_key: str | None,
    automatic_runtime_profile: bool,
    burn_in: bool,
    soft_sub: bool,
    video_codec: str,
    use_intel_gpu: bool,
    video_preset: Any,
    video_crf: Any,
) -> dict[str, Any]:
    if automatic_runtime_profile:
        return {
            "input_key": input_key,
            "srt_key": srt_key,
            "ass_key": None,
            "runtime_profile": True,
            "after_render": {"publish": True, "runtime_profile": True},
        }

    payload: dict[str, Any] = {
        "input_key": input_key,
        "runtime_profile": False,
        "srt_key": srt_key,
        "ass_key": ass_key if burn_in else None,
        "burn_in": bool(burn_in),
        "soft_sub": bool(soft_sub),
        "render": {
            "video_codec": video_codec,
            "use_intel_gpu": use_intel_gpu,
            "video_preset": video_preset,
            "video_crf": video_crf,
        },
    }
    manual_after_render = (
        request_json.get("after_render")
        if isinstance(request_json.get("after_render"), dict)
        else None
    )
    if manual_after_render:
        payload["after_render"] = manual_after_render
    return payload


def store_ass_output(
    *,
    db: Session,
    store: FileStore,
    task_id: uuid.UUID,
    segments: list[Segment],
    ass_path: Path,
    video_path: Path,
    ffmpeg_path: str,
    render_cfg: dict[str, Any],
    bilingual: bool,
    unique_storage_key: UniqueStorageKey,
    log: LogLine,
    log_prefix: str,
) -> str:
    try:
        if not video_path.is_file():
            raise FileNotFoundError(video_path)
        play_res_x, play_res_y = probe_video_resolution(ffmpeg_path, video_path)
    except Exception as error:
        log(f"subtitle ass resolution probe failed; fallback to 1920x1080: {error}")
        play_res_x, play_res_y = 1920, 1080

    ass_text = segments_to_ass(
        segments,
        style_name=render_cfg.get("ass_style", "clean_white"),
        play_res_x=play_res_x,
        play_res_y=play_res_y,
        secondary_line_scale=0.68 if bilingual else None,
        primary_font_scale_percent=render_cfg.get("primary_font_scale_percent") or 100,
        secondary_font_scale_percent=render_cfg.get("secondary_font_scale_percent") or 100,
    )
    ass_path.write_text(ass_text, encoding="utf-8")
    ass_sha = sha256_file(ass_path)
    existing_asset = (
        db.query(Asset)
        .filter(
            Asset.task_id == task_id,
            Asset.kind == AssetKind.subtitle_ass,
            Asset.sha256 == ass_sha,
        )
        .first()
    )
    if existing_asset is not None:
        ass_key = existing_asset.storage_key
        log(f"{log_prefix} unchanged: {ass_key} ({play_res_x}x{play_res_y})")
        return ass_key

    ass_key = unique_storage_key(
        f"sub/{task_id}/subtitle_zh",
        ass_sha,
        ".ass",
    )
    store.upload_file(ass_path, ass_key, content_type="text/plain")
    db.add(
        Asset(
            task_id=task_id,
            kind=AssetKind.subtitle_ass,
            storage_key=ass_key,
            sha256=ass_sha,
            size_bytes=ass_path.stat().st_size,
        )
    )
    existing_subtitle = (
        db.query(Subtitle)
        .filter(
            Subtitle.task_id == task_id,
            Subtitle.format == SubtitleFormat.ass,
            Subtitle.language == "zh",
            Subtitle.storage_key == ass_key,
        )
        .first()
    )
    if existing_subtitle is None:
        db.add(
            Subtitle(
                task_id=task_id,
                version=1,
                format=SubtitleFormat.ass,
                language="zh",
                storage_key=ass_key,
            )
        )
    log(f"{log_prefix}: {ass_key} ({play_res_x}x{play_res_y})")
    return ass_key


def persist_subtitle_outputs(
    *,
    db: Session,
    store: FileStore,
    task: Task,
    job: SubtitleJob,
    request_json: dict[str, Any],
    segments: list[Segment],
    subtitle_segments_path: Path,
    srt_path: Path,
    ass_path: Path,
    video_path: Path,
    ffmpeg_path: str,
    render_cfg: dict[str, Any],
    need_ass: bool,
    ass_bilingual: bool,
    unique_storage_key: UniqueStorageKey,
    clear_translation_checkpoint: NoArgCallback,
    log: LogLine,
) -> SubtitleOutputArtifacts:
    write_json(subtitle_segments_path, segments_to_json_data(segments))
    final_segments_sha = sha256_file(subtitle_segments_path)
    final_segments_key = unique_storage_key(
        f"sub/{task.id}/subtitle_segments",
        final_segments_sha,
        ".json",
    )
    store.upload_file(
        subtitle_segments_path,
        final_segments_key,
        content_type="application/json",
    )
    artifacts = dict(request_json.get("artifacts") or {})
    artifacts["final_subtitle_segments_key"] = final_segments_key
    request_json["artifacts"] = artifacts
    job.request_json = request_json
    db.add(job)
    db.commit()
    log(f"subtitle segments uploaded: {final_segments_key}")

    srt_path.write_text(segments_to_srt(segments), encoding="utf-8")
    srt_sha = sha256_file(srt_path)
    srt_key = unique_storage_key(
        f"sub/{task.id}/subtitle_zh",
        srt_sha,
        ".srt",
    )
    store.upload_file(srt_path, srt_key, content_type="text/plain")
    clear_translation_checkpoint()
    db.add(
        Asset(
            task_id=task.id,
            kind=AssetKind.subtitle_srt,
            storage_key=srt_key,
            sha256=srt_sha,
            size_bytes=srt_path.stat().st_size,
        )
    )
    db.add(
        Subtitle(
            task_id=task.id,
            version=1,
            format=SubtitleFormat.srt,
            language="zh",
            storage_key=srt_key,
        )
    )
    log(f"subtitle srt uploaded: {srt_key}")

    ass_key = None
    if need_ass:
        ass_key = store_ass_output(
            db=db,
            store=store,
            task_id=task.id,
            segments=segments,
            ass_path=ass_path,
            video_path=video_path,
            ffmpeg_path=ffmpeg_path,
            render_cfg=render_cfg,
            bilingual=ass_bilingual,
            unique_storage_key=unique_storage_key,
            log=log,
            log_prefix="subtitle ass uploaded",
        )

    return SubtitleOutputArtifacts(
        final_segments_key=final_segments_key,
        srt_key=srt_key,
        ass_key=ass_key,
    )


def mark_subtitle_ready(
    *,
    db: Session,
    task: Task,
    job: SubtitleJob,
    resume_existing: bool,
) -> None:
    if not resume_existing or task.status in _RESUME_READY_STATUSES:
        task.status = TaskStatus.subtitle_ready
        db.add(task)
    job.progress = 80
    db.add(job)
    db.commit()


def complete_subtitle_handoff(
    *,
    db: Session,
    task: Task,
    job: SubtitleJob,
    automatic_runtime_profile: bool,
    burn_in: bool,
    soft_sub: bool,
    render_payload: dict[str, Any],
    ensure_not_stopped: EnsureNotStopped,
    log: LogLine,
    upload_log: NoArgCallback,
) -> dict[str, str]:
    ensure_not_stopped()

    if not automatic_runtime_profile and not burn_in and not soft_sub:
        job.status = SubtitleJobStatus.succeeded
        job.progress = 100
        db.add(job)
        db.commit()
        log("subtitle job done (no render configured)")
        upload_log()
        return {"status": "ok"}

    render_job = (
        db.query(RenderJob)
        .filter(
            RenderJob.subtitle_job_id == job.id,
            RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]),
        )
        .order_by(RenderJob.created_at.desc())
        .first()
    )
    if render_job is None:
        render_job = RenderJob(
            id=uuid.uuid4(),
            task_id=task.id,
            subtitle_job_id=job.id,
            status=RenderJobStatus.queued,
            progress=0,
            request_json=render_payload,
        )
        db.add(render_job)

    # Render creation and subtitle completion deliberately share one commit so
    # the Render Coordinator never observes an incomplete handoff.
    job.status = SubtitleJobStatus.succeeded
    job.progress = 100
    db.add(job)
    db.commit()
    log("render queued; waiting for render worker")
    upload_log()
    return {"status": "ok", "detail": "render queued", "render_job_id": str(render_job.id)}
