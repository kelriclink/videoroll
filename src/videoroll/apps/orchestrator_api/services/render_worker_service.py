from __future__ import annotations
import hashlib
import ipaddress
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from videoroll.apps.outbox.service import create_outbox_event
from videoroll.apps.orchestrator_api.render_worker_schemas import ArtifactSpec, RenderSpec
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.subtitle_service.processing import (
    probe_video_resolution,
    segments_from_json_data,
    segments_to_ass,
    srt_to_segments,
)
from videoroll.config import get_orchestrator_settings
from videoroll.db.models import AppSetting, Asset, RenderExecution, RenderJob, RenderJobStatus, RenderWorker, RenderWorkerEnrollment, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.storage.filesystem import FileStore, StorageObjectNotFound
from videoroll.utils.auto_youtube import parse_auto_youtube_created_by

LEASE_SECONDS = 180
WORKER_STALE_SECONDS = 180
ACTIVE_EXECUTION_STATES = ("claimed", "running", "uploading")
REMOTE_LOCK_PREFIX = "render-worker:"
ENROLLMENT_PREFIX = "vre_"
WORKER_CREDENTIAL_PREFIX = "vrw_"
RENDER_CONNECTION_SETTINGS_KEY = "render_worker_connection"
LOCAL_WORKER_KEY = "local-render"

def _secret_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

def get_server_url(db: Session, fallback: str) -> str:
    row = db.get(AppSetting, RENDER_CONNECTION_SETTINGS_KEY)
    value = dict(row.value_json or {}).get("server_url") if row else None
    return str(value or fallback).strip().rstrip("/")

def set_server_url(db: Session, value: str) -> str:
    from urllib.parse import urlsplit
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(422, "server_url must be an absolute http(s) URL without credentials")
    row = db.get(AppSetting, RENDER_CONNECTION_SETTINGS_KEY)
    if row is None: row = AppSetting(key=RENDER_CONNECTION_SETTINGS_KEY, value_json={})
    row.value_json = {"server_url": normalized}; row.version = int(row.version or 0) + 1
    db.add(row); db.commit()
    return normalized

def create_enrollment(db: Session, *, label: str, ttl_minutes: int) -> tuple[RenderWorkerEnrollment, str]:
    token = ENROLLMENT_PREFIX + secrets.token_urlsafe(32)
    row = RenderWorkerEnrollment(
        token_hash=_secret_hash(token), label=str(label or "").strip(),
        expires_at=utcnow() + timedelta(minutes=max(5, min(int(ttl_minutes), 1440))),
    )
    db.add(row); db.commit(); db.refresh(row)
    return row, token

def list_enrollments(db: Session) -> list[RenderWorkerEnrollment]:
    return db.query(RenderWorkerEnrollment).order_by(RenderWorkerEnrollment.created_at.desc()).limit(100).all()

def revoke_enrollment(db: Session, enrollment_id: uuid.UUID) -> RenderWorkerEnrollment:
    row = db.query(RenderWorkerEnrollment).filter(RenderWorkerEnrollment.id == enrollment_id).with_for_update().one_or_none()
    if row is None: raise HTTPException(404, "render worker enrollment not found")
    if row.status == "active": row.status = "revoked"
    db.add(row); db.commit(); db.refresh(row)
    return row

def enroll_worker(db: Session, payload: Any) -> tuple[RenderWorker, str]:
    now = utcnow()
    enrollment = (db.query(RenderWorkerEnrollment)
        .filter(RenderWorkerEnrollment.token_hash == _secret_hash(payload.enrollment_token))
        .with_for_update().one_or_none())
    if enrollment is None or enrollment.status != "active" or (_utc(enrollment.expires_at) or now) <= now:
        raise HTTPException(401, "invalid or expired enrollment token")
    worker = db.query(RenderWorker).filter(RenderWorker.worker_key == payload.worker_key).with_for_update().one_or_none()
    existing_max_concurrency = int(worker.max_concurrency) if worker is not None else None
    if worker is None:
        worker = RenderWorker(worker_key=payload.worker_key, name=payload.name, platform=payload.platform)
    for key in ("name","platform","architecture","version","protocol_version","render_spec_versions","capabilities","resources","labels","max_concurrency"):
        setattr(worker, key, getattr(payload, key))
    if existing_max_concurrency is not None:
        worker.max_concurrency = existing_max_concurrency
    credential = WORKER_CREDENTIAL_PREFIX + secrets.token_urlsafe(48)
    worker.credential_hash = _secret_hash(credential)
    worker.credential_issued_at = now
    worker.credential_revoked_at = None
    worker.status = "online"; worker.last_seen_at = now
    db.add(worker); db.flush()
    enrollment.status = "consumed"; enrollment.consumed_at = now; enrollment.worker_id = worker.id
    db.add(enrollment); db.commit(); db.refresh(worker)
    return worker, credential

def authenticate_worker(db: Session, credential: str) -> RenderWorker:
    row = db.query(RenderWorker).filter(RenderWorker.credential_hash == _secret_hash(credential)).one_or_none()
    if row is None or row.credential_revoked_at is not None or not row.enabled:
        raise HTTPException(401, "invalid render worker credential")
    return row

def is_private_worker_bootstrap_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def enroll_local_worker(db: Session, payload: Any) -> tuple[RenderWorker, str]:
    now = utcnow()
    worker = db.query(RenderWorker).filter(RenderWorker.worker_key == payload.worker_key).with_for_update().one_or_none()
    existing_max_concurrency = int(worker.max_concurrency) if worker is not None else None
    if worker is None:
        worker = RenderWorker(worker_key=payload.worker_key, name=payload.name, platform=payload.platform)
    for key in ("name","platform","architecture","version","protocol_version","render_spec_versions","capabilities","resources","labels","max_concurrency"):
        setattr(worker, key, getattr(payload, key))
    if existing_max_concurrency is not None:
        worker.max_concurrency = existing_max_concurrency
    credential = WORKER_CREDENTIAL_PREFIX + secrets.token_urlsafe(48)
    worker.credential_hash = _secret_hash(credential)
    worker.credential_issued_at = now
    worker.credential_revoked_at = None
    worker.status = "online"
    worker.enabled = True
    worker.last_seen_at = now
    worker.labels = {**dict(worker.labels or {}), "local": True, "standalone": True}
    db.add(worker); db.commit(); db.refresh(worker)
    return worker, credential


def revoke_worker_credential(db: Session, worker_id: uuid.UUID) -> RenderWorker:
    row = get_worker(db, worker_id, lock=True)
    row.credential_revoked_at = utcnow(); row.credential_hash = None
    row.status = "offline"; row.enabled = False
    db.add(row); db.commit(); db.refresh(row)
    return row

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def register_worker(db: Session, payload: Any) -> RenderWorker:
    row = db.query(RenderWorker).filter(RenderWorker.worker_key == payload.worker_key).one_or_none()
    if row is None:
        row = RenderWorker(worker_key=payload.worker_key, name=payload.name, platform=payload.platform)
    for key in ("name","platform","architecture","version","protocol_version","render_spec_versions","capabilities","resources","labels","max_concurrency"):
        setattr(row, key, getattr(payload, key))
    row.status = "online"
    row.last_seen_at = utcnow()
    db.add(row); db.commit(); db.refresh(row)
    return row

def ensure_local_worker(db: Session, *, capabilities: dict[str, Any], resources: dict[str, Any] | None = None) -> RenderWorker:
    row = db.query(RenderWorker).filter(RenderWorker.worker_key == LOCAL_WORKER_KEY).with_for_update().one_or_none()
    if row is None:
        row = RenderWorker(
            worker_key=LOCAL_WORKER_KEY, name="本机渲染节点", platform="linux",
            architecture=None, version="builtin", protocol_version=1, render_spec_versions=[1],
            capabilities=capabilities, resources=resources or {}, labels={"local": True},
            status="online", enabled=True, draining=False, max_concurrency=1, active_jobs=0,
        )
    else:
        row.capabilities = capabilities
        row.resources = resources or row.resources or {}
        row.labels = {**dict(row.labels or {}), "local": True}
        row.last_seen_at = utcnow()
        if row.status == "offline" and row.enabled:
            row.status = "online"
    db.add(row)
    db.flush()
    reconcile_worker_executions(db, row.id)
    row.active_jobs = db.query(RenderExecution).filter(
        RenderExecution.worker_id == row.id, RenderExecution.state.in_(ACTIVE_EXECUTION_STATES)
    ).count()
    db.add(row); db.commit(); db.refresh(row)
    return row

def get_worker(db: Session, worker_id: uuid.UUID, *, lock: bool = False) -> RenderWorker:
    q = db.query(RenderWorker).filter(RenderWorker.id == worker_id)
    if lock: q = q.with_for_update()
    row = q.one_or_none()
    if row is None: raise HTTPException(404, "render worker not found")
    return row

def reconcile_worker_executions(db: Session, worker_id: uuid.UUID) -> int:
    now = utcnow()
    rows = (db.query(RenderExecution)
        .filter(RenderExecution.worker_id == worker_id, RenderExecution.state.in_(ACTIVE_EXECUTION_STATES),
                RenderExecution.lease_until.is_not(None), RenderExecution.lease_until <= now)
        .with_for_update(skip_locked=True).all())
    for execution in rows:
        execution.state = "lost"
        execution.error_message = execution.error_message or "render execution lease expired"
        execution.finished_at = now
        db.add(execution)
    if rows:
        db.flush()
    return len(rows)

def heartbeat_worker(db: Session, worker_id: uuid.UUID, payload: Any) -> RenderWorker:
    row = get_worker(db, worker_id, lock=True)
    row.status = payload.status
    row.draining = payload.status == "draining" or row.draining
    row.resources = payload.resources
    if payload.capabilities is not None: row.capabilities = payload.capabilities
    row.active_jobs = db.query(RenderExecution).filter(RenderExecution.worker_id == row.id, RenderExecution.state.in_(ACTIVE_EXECUTION_STATES)).count()
    row.last_seen_at = utcnow()
    db.add(row); db.commit(); db.refresh(row)
    return row

def control_worker(db: Session, worker_id: uuid.UUID, payload: Any) -> RenderWorker:
    row = get_worker(db, worker_id, lock=True)
    for key in ("enabled","draining","max_concurrency"):
        value = getattr(payload, key)
        if value is not None: setattr(row, key, value)
    if getattr(payload, "worker_key", None) is not None:
        worker_key = str(payload.worker_key).strip()
        if not worker_key:
            raise HTTPException(400, "render worker key must not be blank")
        duplicate = (
            db.query(RenderWorker)
            .filter(RenderWorker.worker_key == worker_key, RenderWorker.id != row.id)
            .one_or_none()
        )
        if duplicate is not None:
            raise HTTPException(409, "render worker key is already in use")
        row.worker_key = worker_key
    db.add(row); db.commit(); db.refresh(row)
    return row

def list_workers(db: Session) -> list[RenderWorker]:
    return db.query(RenderWorker).order_by(RenderWorker.last_seen_at.desc()).all()

def worker_admin_payload(worker: RenderWorker) -> dict[str, Any]:
    seen = _utc(worker.last_seen_at) or utcnow()
    age = max(0, int((utcnow() - seen).total_seconds()))
    resources = worker.resources or {}
    devices = resources.get("devices") if isinstance(resources.get("devices"), list) else []
    device_count = len([device for device in devices if isinstance(device, dict)])
    effective_capacity = int(worker.max_concurrency or 0)
    available_slots = max(0, effective_capacity - int(worker.active_jobs or 0))
    return {
        "id": worker.id, "worker_key": worker.worker_key, "name": worker.name, "platform": worker.platform,
        "architecture": worker.architecture, "version": worker.version, "protocol_version": worker.protocol_version,
        "render_spec_versions": worker.render_spec_versions or [], "capabilities": worker.capabilities or {},
        "resources": worker.resources or {}, "labels": worker.labels or {}, "status": worker.status,
        "enabled": worker.enabled, "draining": worker.draining, "max_concurrency": worker.max_concurrency,
        "active_jobs": worker.active_jobs, "device_count": device_count,
        # Legacy field retained for API compatibility. It now represents the
        # number of detected schedulable devices, not a hard concurrency cap.
        "detected_capacity": device_count,
        "effective_capacity": effective_capacity, "available_slots": available_slots,
        "last_seen_at": worker.last_seen_at,
        "stale": age > WORKER_STALE_SECONDS, "seconds_since_heartbeat": age,
        "credential_active": bool(worker.credential_hash and worker.credential_revoked_at is None),
    }

def list_executions(
    db: Session,
    *,
    worker_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    state: str | None = None,
    limit: int = 100,
) -> list[RenderExecution]:
    q = db.query(RenderExecution)
    if worker_id is not None:
        q = q.filter(RenderExecution.worker_id == worker_id)
    if task_id is not None:
        q = q.join(RenderJob, RenderJob.id == RenderExecution.render_job_id).filter(RenderJob.task_id == task_id)
    if state:
        q = q.filter(RenderExecution.state == state)
    return q.order_by(RenderExecution.created_at.desc()).limit(max(1, min(limit, 500))).all()

def execution_admin_payload(db: Session, execution: RenderExecution) -> dict[str, Any]:
    worker = db.get(RenderWorker, execution.worker_id)
    job = db.get(RenderJob, execution.render_job_id)
    return {
        "id": execution.id, "render_job_id": execution.render_job_id, "worker_id": execution.worker_id,
        "attempt": execution.attempt, "state": execution.state,
        "transfer_mode": execution.transfer_mode, "progress": execution.progress,
        "lease_until": execution.lease_until, "render_spec": execution.render_spec,
        "worker_name": worker.name if worker else None, "task_id": job.task_id if job else None,
        "job_status": job.status.value if job and hasattr(job.status, "value") else (str(job.status) if job else None),
        "metrics": execution.metrics or {}, "log_tail": execution.log_tail, "error_message": execution.error_message,
        "heartbeat_at": execution.heartbeat_at, "started_at": execution.started_at, "finished_at": execution.finished_at,
    }

def _supports_job(worker: RenderWorker, job: RenderJob, available_encoders: set[str] | None = None) -> bool:
    if 1 not in (worker.render_spec_versions or []): return False
    caps = worker.capabilities or {}
    encoders = (
        {str(x).lower() for x in available_encoders if x}
        if available_encoders is not None
        else {str(x).lower() for x in caps.get("encoders", []) if x}
    )
    req = job.request_json if isinstance(job.request_json, dict) else {}
    render = req.get("render") if isinstance(req.get("render"), dict) else {}
    codec = str(render.get("video_codec") or "av1").lower()
    if not encoders and available_encoders is None:
        return True  # compatibility for early workers; execution-side probe remains authoritative
    if not encoders:
        return False
    if codec == "av1":
        return bool(encoders & {"av1_nvenc","av1_vaapi","av1_qsv","libsvtav1","libaom-av1"})
    if codec in {"h264","avc"}:
        return bool(encoders & {"h264_nvenc","h264_vaapi","h264_qsv","libx264"})
    if codec in {"hevc","h265"}:
        return bool(encoders & {"hevc_nvenc","hevc_vaapi","hevc_qsv","libx265"})
    return True

def _artifact(db: Session, store: FileStore, role: str, key: str, execution_id: uuid.UUID) -> ArtifactSpec:
    try:
        head = store.head_object(key)
    except StorageObjectNotFound as exc:
        raise HTTPException(409, f"render input missing: {role}") from exc
    asset = db.query(Asset).filter(Asset.storage_key == key).order_by(Asset.created_at.desc()).first()
    sha256 = asset.sha256 if asset is not None else None
    return ArtifactSpec(role=role, storage_key=key, size_bytes=int(head.get("ContentLength") or 0), sha256=sha256,
        download_url=f"/render-workers/v1/executions/{execution_id}/artifacts/{role}")

def _build_spec(db: Session, store: FileStore, job: RenderJob, execution_id: uuid.UUID) -> RenderSpec:
    req = dict(job.request_json or {})
    task = db.get(Task, job.task_id)
    automatic = bool(req.get("runtime_profile")) if "runtime_profile" in req else bool(
        task is not None and parse_auto_youtube_created_by(task.created_by) is not None
    )
    if automatic:
        profile = dict(get_auto_profile(db))
        req["runtime_profile"] = True
        req["burn_in"] = bool(profile.get("burn_in"))
        req["soft_sub"] = bool(profile.get("soft_sub"))
        req["render"] = {
            "video_codec": profile.get("video_codec") or "av1",
            # Kept for wire compatibility; a remote backend may map this policy
            # to NVENC/VAAPI/QSV according to its advertised capabilities.
            "use_intel_gpu": bool(profile.get("use_intel_gpu")),
            "video_preset": profile.get("video_preset"),
            "video_crf": profile.get("video_crf"),
            "ass_style": profile.get("ass_style") or "clean_white",
            "primary_font_scale_percent": profile.get("primary_font_scale_percent") or 100,
            "secondary_font_scale_percent": profile.get("secondary_font_scale_percent") or 100,
        }
    burn_in, soft_sub = bool(req.get("burn_in")), bool(req.get("soft_sub"))
    mode = "burn_in" if burn_in else ("soft_sub" if soft_sub else "noop")
    artifacts: list[ArtifactSpec] = []
    for role, field in (("input","input_key"),("srt","srt_key"),("ass","ass_key")):
        key = str(req.get(field) or "").strip()
        if key: artifacts.append(_artifact(db, store, role, key, execution_id))

    # A standalone worker must receive an executable render contract and must
    # never query the coordinator database. Automatic renders historically
    # generated ASS inside subtitle-worker at execution time; materialize the
    # same ASS here and expose it as a normal execution-scoped artifact.
    if automatic and burn_in and not str(req.get("ass_key") or "").strip():
        input_key = str(req.get("input_key") or "").strip()
        srt_key = str(req.get("srt_key") or "").strip()
        if not input_key or not srt_key:
            raise HTTPException(409, "automatic burn-in requires input and SRT artifacts")
        runtime_segments = []
        if job.subtitle_job_id:
            subtitle_job = db.get(SubtitleJob, job.subtitle_job_id)
            subtitle_request = subtitle_job.request_json if subtitle_job and isinstance(subtitle_job.request_json, dict) else {}
            segment_key = str(dict(subtitle_request.get("artifacts") or {}).get("final_subtitle_segments_key") or "").strip()
            if segment_key:
                try:
                    import json
                    runtime_segments = segments_from_json_data(json.loads(store.path_for(segment_key).read_text(encoding="utf-8")))
                except Exception:
                    runtime_segments = []
        if not runtime_segments:
            runtime_segments = srt_to_segments(store.path_for(srt_key).read_text(encoding="utf-8"))
        ffmpeg_path = get_orchestrator_settings().ffmpeg_path
        play_res_x, play_res_y = probe_video_resolution(ffmpeg_path, store.path_for(input_key))
        render_cfg = req.get("render") if isinstance(req.get("render"), dict) else {}
        secondary_line_scale = 0.68 if any(segment.secondary_text for segment in runtime_segments) else None
        ass_text = segments_to_ass(
            runtime_segments,
            style_name=str(render_cfg.get("ass_style") or "clean_white"),
            play_res_x=play_res_x,
            play_res_y=play_res_y,
            secondary_line_scale=secondary_line_scale,
            primary_font_scale_percent=int(render_cfg.get("primary_font_scale_percent") or 100),
            secondary_font_scale_percent=int(render_cfg.get("secondary_font_scale_percent") or 100),
        )
        runtime_ass_key = f"render-input/{execution_id}/subtitle_runtime.ass"
        runtime_ass_path = store.path_for(runtime_ass_key, require_exists=False)
        runtime_ass_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_ass_path.write_text(ass_text, encoding="utf-8")
        req["ass_key"] = runtime_ass_key
        artifacts = [artifact for artifact in artifacts if artifact.role != "ass"]
        artifacts.append(_artifact(db, store, "ass", runtime_ass_key, execution_id))
    return RenderSpec(schema_version=1, render_job_id=job.id, task_id=job.task_id, mode=mode, request=req, artifacts=artifacts)

def claim_job(
    db: Session,
    store: FileStore,
    worker_id: uuid.UUID,
    accepted_transfer_modes: list[str],
    available_encoders: list[str] | None = None,
) -> tuple[RenderExecution | None, RenderSpec | None]:
    worker = get_worker(db, worker_id, lock=True)
    now = utcnow()
    if not worker.enabled or worker.draining or worker.status == "paused": return None, None
    if "http" not in accepted_transfer_modes:
        # Mapped-storage is reserved in the wire schema but is not advertised as
        # executable until server-side path translation is configured.
        return None, None
    reconcile_worker_executions(db, worker.id)
    active = db.query(RenderExecution).filter(RenderExecution.worker_id == worker.id, RenderExecution.state.in_(ACTIVE_EXECUTION_STATES)).count()
    if active >= worker.max_concurrency: return None, None
    jobs = (db.query(RenderJob).join(Task, Task.id == RenderJob.task_id)
        .filter(RenderJob.status == RenderJobStatus.queued,
                or_(RenderJob.lease_until.is_(None), RenderJob.lease_until <= now),
                Task.status.notin_([TaskStatus.canceled, TaskStatus.published]),
                or_(Task.lock_until.is_(None), Task.lock_until <= now, Task.lock_owner.like(f"{REMOTE_LOCK_PREFIX}%")))
        .order_by(Task.priority.desc(), Task.queue_position.asc().nullslast(), RenderJob.created_at.asc())
        .with_for_update(skip_locked=True).limit(32).all())
    free_encoders = {str(x).lower() for x in available_encoders} if available_encoders is not None else None
    job = next((candidate for candidate in jobs if _supports_job(worker, candidate, free_encoders)), None)
    if job is None: return None, None
    task = db.get(Task, job.task_id)
    attempt = int(db.query(func.max(RenderExecution.attempt)).filter(RenderExecution.render_job_id == job.id).scalar() or 0) + 1
    execution = RenderExecution(render_job_id=job.id, worker_id=worker.id, attempt=attempt,
        fence_token=secrets.token_hex(24), state="claimed", transfer_mode="http",
        capability_snapshot=dict(worker.capabilities or {}), lease_until=now + timedelta(seconds=LEASE_SECONDS), heartbeat_at=now)
    db.add(execution); db.flush()
    spec = _build_spec(db, store, job, execution.id)
    execution.render_spec = spec.model_dump(mode="json")
    owner = f"{REMOTE_LOCK_PREFIX}{execution.id}"
    job.status = RenderJobStatus.running; job.started_at = job.started_at or now; job.progress = max(int(job.progress or 0), 2)
    job.lease_owner = owner; job.lease_until = execution.lease_until; job.heartbeat_at = now
    if task is not None:
        task.lock_owner = owner; task.lock_until = execution.lease_until; db.add(task)
    worker.active_jobs = active + 1; worker.status = "busy"; worker.last_seen_at = now
    db.add_all([job, execution, worker]); db.commit(); db.refresh(execution)
    return execution, spec

def get_execution(db: Session, execution_id: uuid.UUID, *, lock: bool = False) -> RenderExecution:
    q = db.query(RenderExecution).filter(RenderExecution.id == execution_id)
    if lock: q = q.with_for_update()
    row = q.one_or_none()
    if row is None: raise HTTPException(404, "render execution not found")
    return row

def validate_fence(execution: RenderExecution, fence_token: str) -> None:
    if not secrets.compare_digest(str(execution.fence_token), str(fence_token)):
        raise HTTPException(409, "stale render execution fence token")
    if execution.state not in ACTIVE_EXECUTION_STATES:
        raise HTTPException(409, f"render execution is {execution.state}")

def heartbeat_execution(db: Session, execution_id: uuid.UUID, payload: Any) -> RenderExecution:
    execution = get_execution(db, execution_id, lock=True); validate_fence(execution, payload.fence_token)
    now = utcnow()
    lease_until = _utc(execution.lease_until)
    if lease_until is not None and lease_until <= now: raise HTTPException(409, "render execution lease expired")
    job = db.get(RenderJob, execution.render_job_id)
    if job is None or job.lease_owner != f"{REMOTE_LOCK_PREFIX}{execution.id}": raise HTTPException(409, "render job ownership lost")
    execution.state = "running"; execution.heartbeat_at = now; execution.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    execution.metrics = payload.metrics
    if payload.progress is not None: execution.progress = payload.progress
    # Persist the execution lease first. Periodic heartbeats must not be held
    # hostage by unrelated RenderJob/Task row locks or recovery bookkeeping.
    db.add(execution); db.commit(); db.refresh(execution)
    if payload.progress is None:
        return execution

    # Progress propagation is secondary to lease renewal. If this commit waits
    # on a business row lock, the execution lease above is already durable.
    job = db.get(RenderJob, execution.render_job_id)
    if job is None or job.lease_owner != f"{REMOTE_LOCK_PREFIX}{execution.id}":
        raise HTTPException(409, "render job ownership lost")
    job.heartbeat_at = now
    job.lease_until = execution.lease_until
    job.progress = payload.progress
    db.add(job); db.commit()
    return execution

def append_log(db: Session, execution_id: uuid.UUID, fence_token: str, text: str) -> RenderExecution:
    execution = get_execution(db, execution_id, lock=True); validate_fence(execution, fence_token)
    execution.log_tail = ((execution.log_tail or "") + text)[-262144:]
    db.add(execution); db.commit(); db.refresh(execution); return execution

def settle_local_execution(
    db: Session, execution_id: uuid.UUID, fence_token: str, *, succeeded: bool, error: str | None = None
) -> RenderExecution:
    """Close coordinator state after the in-process local renderer finalizes business state."""
    execution = get_execution(db, execution_id, lock=True)
    validate_fence(execution, fence_token)
    job = db.get(RenderJob, execution.render_job_id)
    owner = f"{REMOTE_LOCK_PREFIX}{execution.id}"
    if job is None or job.lease_owner != owner:
        raise HTTPException(409, "render job ownership lost")
    now = utcnow()
    execution.state = "succeeded" if succeeded else "failed"
    execution.progress = 100 if succeeded else execution.progress
    execution.error_message = error
    execution.finished_at = now
    job.lease_owner = None; job.lease_until = None; job.heartbeat_at = now
    task = db.get(Task, job.task_id)
    if task is not None and task.lock_owner == owner:
        task.lock_owner = None; task.lock_until = None; db.add(task)
    worker = db.get(RenderWorker, execution.worker_id)
    if worker is not None:
        worker.active_jobs = max(0, int(worker.active_jobs or 0) - 1)
        worker.status = "online"; worker.last_seen_at = now; db.add(worker)
    db.add_all([execution, job]); db.commit(); db.refresh(execution)
    return execution

def artifact_key_for_role(execution: RenderExecution, role: str) -> str:
    for item in (execution.render_spec or {}).get("artifacts", []):
        if isinstance(item, dict) and item.get("role") == role: return str(item.get("storage_key") or "")
    raise HTTPException(404, "render artifact not found")

def complete_execution(db: Session, execution_id: uuid.UUID, payload: Any) -> RenderExecution:
    execution = get_execution(db, execution_id, lock=True); validate_fence(execution, payload.fence_token)
    job = db.get(RenderJob, execution.render_job_id)
    if job is None or job.lease_owner != f"{REMOTE_LOCK_PREFIX}{execution.id}": raise HTTPException(409, "render job ownership lost")
    mode = str((execution.render_spec or {}).get("mode") or "noop")
    if mode != "noop" and payload.output_asset_id is None:
        raise HTTPException(400, "render output asset is required")
    if payload.output_asset_id is not None:
        asset = db.get(Asset, payload.output_asset_id)
        if asset is None or asset.task_id != job.task_id: raise HTTPException(400, "output asset does not belong to render task")
    now = utcnow(); execution.state = "succeeded"; execution.progress = 100; execution.output_json = payload.output; execution.finished_at = now
    job.status = RenderJobStatus.succeeded; job.progress = 100; job.finished_at = now; job.lease_owner = None; job.lease_until = None; job.heartbeat_at = now
    if job.subtitle_job_id:
        subtitle_job = db.get(SubtitleJob, job.subtitle_job_id)
        if subtitle_job is not None:
            subtitle_job.status = SubtitleJobStatus.succeeded
            subtitle_job.progress = 100
            subtitle_job.error_message = None
            db.add(subtitle_job)
    task = db.get(Task, job.task_id)
    if task is not None:
        task.status = TaskStatus.rendered
        if task.lock_owner == f"{REMOTE_LOCK_PREFIX}{execution.id}": task.lock_owner = None; task.lock_until = None
        db.add(task)
    req = job.request_json if isinstance(job.request_json, dict) else {}
    after_render = req.get("after_render") if isinstance(req, dict) else None
    automatic = bool(req.get("runtime_profile")) if "runtime_profile" in req else bool(
        task is not None and parse_auto_youtube_created_by(task.created_by) is not None
    )
    if automatic or (isinstance(after_render, dict) and after_render.get("publish")):
        create_outbox_event(db, event_type="render.after_publish", aggregate_type="render_job", aggregate_id=job.id,
            task_name="subtitle_service.after_render_publish", args={"args":[str(job.id)],"queue":"subtitle"},
            operation_key=f"after-render-publish:{job.id}")
    worker = db.get(RenderWorker, execution.worker_id)
    if worker is not None: worker.active_jobs = max(0, int(worker.active_jobs or 0)-1); worker.status = "online"; db.add(worker)
    db.add_all([execution, job]); db.commit(); db.refresh(execution); return execution

def fail_execution(db: Session, execution_id: uuid.UUID, payload: Any) -> RenderExecution:
    execution = get_execution(db, execution_id, lock=True); validate_fence(execution, payload.fence_token)
    job = db.get(RenderJob, execution.render_job_id); now = utcnow()
    execution.state = "failed"; execution.error_message = payload.error; execution.finished_at = now
    if job is not None and job.lease_owner == f"{REMOTE_LOCK_PREFIX}{execution.id}":
        job.lease_owner = None; job.lease_until = None; job.heartbeat_at = now; job.error_message = payload.error
        job.status = RenderJobStatus.queued if payload.retryable else RenderJobStatus.failed
        if payload.retryable: job.retry_count = int(job.retry_count or 0)+1; job.progress = 0; job.started_at = None
        else: job.finished_at = now
        task = db.get(Task, job.task_id)
        if task is not None:
            if not payload.retryable and task.status != TaskStatus.published:
                task.status = TaskStatus.failed
                task.error_code = task.error_code or "RENDER_FAILED"
                task.error_message = payload.error
            if task.lock_owner == f"{REMOTE_LOCK_PREFIX}{execution.id}":
                task.lock_owner = None; task.lock_until = None
            db.add(task)
        if not payload.retryable and job.subtitle_job_id:
            subtitle_job = db.get(SubtitleJob, job.subtitle_job_id)
            if subtitle_job is not None:
                subtitle_job.status = SubtitleJobStatus.failed
                subtitle_job.error_message = f"render failed: {payload.error}"
                db.add(subtitle_job)
        db.add(job)
    worker = db.get(RenderWorker, execution.worker_id)
    if worker is not None: worker.active_jobs = max(0, int(worker.active_jobs or 0)-1); worker.status = "online"; db.add(worker)
    db.add(execution); db.commit(); db.refresh(execution); return execution

def cancellation_state(db: Session, execution_id: uuid.UUID) -> tuple[bool, str | None]:
    execution = get_execution(db, execution_id); job = db.get(RenderJob, execution.render_job_id)
    if execution.state not in ACTIVE_EXECUTION_STATES: return True, f"execution is {execution.state}"
    lease_until = _utc(execution.lease_until)
    if lease_until is not None and lease_until <= utcnow(): return True, "render execution lease expired"
    if job is None or job.status == RenderJobStatus.canceled: return True, "render job canceled"
    task = db.get(Task, job.task_id)
    if task is None or task.status in {TaskStatus.canceled, TaskStatus.published}: return True, "task stopped or terminal"
    return False, None

def cancel_execution(db: Session, execution_id: uuid.UUID, reason: str) -> RenderExecution:
    execution = get_execution(db, execution_id, lock=True)
    if execution.state not in ACTIVE_EXECUTION_STATES:
        return execution
    now = utcnow()
    execution.state = "canceled"
    execution.error_message = reason
    execution.finished_at = now
    job = db.get(RenderJob, execution.render_job_id)
    owner = f"{REMOTE_LOCK_PREFIX}{execution.id}"
    if job is not None and job.lease_owner == owner:
        job.status = RenderJobStatus.canceled
        job.error_message = reason
        job.finished_at = now
        job.lease_owner = None; job.lease_until = None; job.heartbeat_at = now
        task = db.get(Task, job.task_id)
        if task is not None and task.lock_owner == owner:
            task.lock_owner = None; task.lock_until = None; db.add(task)
        db.add(job)
    worker = db.get(RenderWorker, execution.worker_id)
    if worker is not None:
        worker.active_jobs = max(0, int(worker.active_jobs or 0) - 1)
        worker.status = "online"; db.add(worker)
    db.add(execution); db.commit(); db.refresh(execution)
    return execution

def requeue_execution(db: Session, execution_id: uuid.UUID, reason: str) -> RenderExecution:
    execution = get_execution(db, execution_id, lock=True)
    job = db.get(RenderJob, execution.render_job_id)
    if job is None:
        raise HTTPException(404, "render job not found")
    now = utcnow()
    owner = f"{REMOTE_LOCK_PREFIX}{execution.id}"
    if execution.state in ACTIVE_EXECUTION_STATES:
        execution.state = "canceled"
        execution.finished_at = now
        execution.error_message = reason
    job.status = RenderJobStatus.queued
    job.progress = 0; job.started_at = None; job.finished_at = None
    job.error_message = reason; job.retry_count = int(job.retry_count or 0) + 1
    if job.lease_owner == owner or job.lease_owner is None:
        job.lease_owner = None; job.lease_until = None; job.heartbeat_at = now
    else:
        raise HTTPException(409, "render job is owned by another execution")
    task = db.get(Task, job.task_id)
    if task is not None and task.lock_owner == owner:
        task.lock_owner = None; task.lock_until = None; db.add(task)
    worker = db.get(RenderWorker, execution.worker_id)
    if worker is not None:
        worker.active_jobs = max(0, int(worker.active_jobs or 0) - 1)
        worker.status = "online"; db.add(worker)
    db.add_all([execution, job]); db.commit(); db.refresh(execution)
    return execution
