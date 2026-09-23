from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from videoroll.db.base import Base


JSON_PAYLOAD = JSON().with_variant(JSONB(), "postgresql")


class SourceType(str, enum.Enum):
    youtube = "youtube"
    local = "local"
    url = "url"


class SourceLicense(str, enum.Enum):
    own = "own"
    authorized = "authorized"
    cc = "cc"
    unknown = "unknown"


class TaskStatus(str, enum.Enum):
    created = "CREATED"
    ingested = "INGESTED"
    downloaded = "DOWNLOADED"
    audio_extracted = "AUDIO_EXTRACTED"
    asr_done = "ASR_DONE"
    translated = "TRANSLATED"
    subtitle_ready = "SUBTITLE_READY"
    rendered = "RENDERED"
    ready_for_review = "READY_FOR_REVIEW"
    approved = "APPROVED"
    publishing = "PUBLISHING"
    published = "PUBLISHED"
    failed = "FAILED"
    canceled = "CANCELED"


class AssetKind(str, enum.Enum):
    video_raw = "video_raw"
    metadata_json = "metadata_json"
    audio_wav = "audio_wav"
    segments_json = "segments_json"
    subtitle_srt = "subtitle_srt"
    subtitle_ass = "subtitle_ass"
    video_final = "video_final"
    cover_image = "cover_image"
    log = "log"
    publish_result = "publish_result"


class SubtitleFormat(str, enum.Enum):
    srt = "srt"
    vtt = "vtt"
    ass = "ass"


class PublishState(str, enum.Enum):
    draft = "draft"
    submitting = "submitting"
    submitted = "submitted"
    published = "published"
    unknown = "unknown"
    failed = "failed"


class PublishBatch(Base):
    __tablename__ = "publish_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)

    # Kept as strings instead of a database enum so a new deployment does not
    # need an enum migration before it can create the batch table.
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    expected_targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON_PAYLOAD, nullable=False, default=list)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    outcomes_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)

    cleanup_enqueued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Versioned so deployment can safely replay cleanup markers produced by the
    # earlier best-effort dispatcher once, without replaying every restart.
    cleanup_delivery_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    task: Mapped["Task"] = relationship(back_populates="publish_batches")
    publish_jobs: Mapped[list["PublishJob"]] = relationship(back_populates="batch")

    __table_args__ = (
        Index("ix_publish_batches_task_state", "task_id", "state"),
    )


class Platform(str, enum.Enum):
    bilibili = "bilibili"
    youtube = "youtube"
    douyin = "douyin"
    xiaohongshu = "xiaohongshu"
    kuaishou = "kuaishou"
    tencent = "tencent"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType, name="source_type"), nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source_license: Mapped[SourceLicense] = mapped_column(Enum(SourceLicense, name="source_license"), nullable=False)
    source_proof_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.created)
    # A stopped task uses the existing CANCELED state while retaining the
    # workflow stage it should return to when the user resumes it.
    stopped_status: Mapped[Optional[TaskStatus]] = mapped_column(Enum(TaskStatus, name="task_status"), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Compatibility pointer for legacy rows.  New publishing resolves the
    # current batch per platform/account target so Bilibili and social
    # platforms can run at the same time.
    active_publish_batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    assets: Mapped[list["Asset"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    subtitles: Mapped[list["Subtitle"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    publish_jobs: Mapped[list["PublishJob"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    publish_batches: Mapped[list["PublishBatch"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    pipeline_runs: Mapped[list["PipelineRun"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_tasks_status_created_at", "status", "created_at"),
        Index("ix_tasks_priority_created_at", "priority", "created_at"),
        Index("ix_tasks_active_publish_batch_id", "active_publish_batch_id"),
    )


class PipelineRun(Base):
    """VideoRoll-to-execution-engine run mapping.

    This table is deliberately not a scheduler: it contains no lease, heartbeat,
    retry-owner, or worker-allocation fields. Runtime truth belongs to the
    selected workflow engine; VideoRoll keeps only the business correlation and
    audit history needed to correlate VideoRoll business tasks with Hatchet.
    """

    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    workflow_name: Mapped[str] = mapped_column(String(128), nullable=False)
    external_run_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="submitting", server_default="submitting")
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    task: Mapped["Task"] = relationship(back_populates="pipeline_runs")

    __table_args__ = (
        UniqueConstraint("engine", "external_run_id", name="uq_pipeline_runs_engine_external_run"),
        Index("ix_pipeline_runs_task_created", "task_id", "created_at"),
        Index("ix_pipeline_runs_state_created", "state", "created_at"),
    )


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)

    kind: Mapped[AssetKind] = mapped_column(Enum(AssetKind, name="asset_kind"), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)

    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="assets")

    __table_args__ = (
        Index("ix_assets_task_kind", "task_id", "kind"),
    )


class PlayoutAssetLink(Base):
    """Server-side mapping from a VideoRoll asset to ffplayout media."""

    __tablename__ = "playout_asset_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)

    ffplayout_channel_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    relative_media_path: Mapped[str] = mapped_column(Text, nullable=False)
    transfer_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("asset_id", name="uq_playout_asset_links_asset_id"),
        UniqueConstraint(
            "ffplayout_channel_id",
            "relative_media_path",
            name="uq_playout_asset_links_channel_path",
        ),
        Index("ix_playout_asset_links_task", "task_id"),
    )


class Subtitle(Base):
    __tablename__ = "subtitles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    format: Mapped[SubtitleFormat] = mapped_column(Enum(SubtitleFormat, name="subtitle_format"), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh")
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)

    editor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="subtitles")

    __table_args__ = (
        Index("ix_subtitles_task_version", "task_id", "version"),
    )


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("publish_batches.id", ondelete="SET NULL"), nullable=True
    )

    platform: Mapped[Platform] = mapped_column(Enum(Platform, name="platform"), nullable=False, default=Platform.bilibili)
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    bili_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)

    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    cover_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    state: Mapped[PublishState] = mapped_column(Enum(PublishState, name="publish_state"), nullable=False, default=PublishState.draft)
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    external_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bvid: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    aid: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    response_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON_PAYLOAD, nullable=True)

    # These fields track only the Bilibili video transfer.  They deliberately
    # do not represent the overall publish lifecycle, which also includes
    # metadata selection and archive submission.
    upload_progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0..100
    upload_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    task: Mapped["Task"] = relationship(back_populates="publish_jobs")
    batch: Mapped[Optional["PublishBatch"]] = relationship(back_populates="publish_jobs")
    account: Mapped[Optional["Account"]] = relationship(foreign_keys=[account_id])
    bili_account: Mapped[Optional["Account"]] = relationship(foreign_keys=[bili_account_id])

    __table_args__ = (
        Index("ix_publish_jobs_task_state", "task_id", "state"),
        Index("ix_publish_jobs_platform_state", "platform", "state"),
        Index("ix_publish_jobs_batch_platform_account", "batch_id", "platform", "account_id"),
        Index("ix_publish_jobs_state_lease_until", "state", "lease_until", "created_at"),
        UniqueConstraint("operation_key", name="uq_publish_jobs_operation_key"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[Platform] = mapped_column(Enum(Platform, name="platform"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    secrets_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    check_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unchecked")
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("platform", "name", name="uq_accounts_platform_name"),
    )


class SubtitleJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class RenderJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class RenderJob(Base):
    __tablename__ = "render_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    subtitle_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subtitle_jobs.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[RenderJobStatus] = mapped_column(Enum(RenderJobStatus, name="render_job_status"), nullable=False, default=RenderJobStatus.queued)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0..100
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_render_jobs_status_created_at", "status", "created_at"),
        Index("ix_render_jobs_task_status", "task_id", "status"),
        Index("ix_render_jobs_subtitle_job", "subtitle_job_id"),
        Index("ix_render_jobs_status_lease_until", "status", "lease_until", "created_at"),
        Index("ix_render_jobs_operation_key", "operation_key"),
    )


class RenderWorker(Base):
    __tablename__ = "render_workers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    architecture: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    render_spec_versions: Mapped[list[int]] = mapped_column(JSON_PAYLOAD, nullable=False, default=lambda: [1])
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    resources: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="online")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    draining: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credential_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    credential_issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    credential_revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_render_workers_status_seen", "status", "last_seen_at"),
        Index("ix_render_workers_enabled_draining", "enabled", "draining"),
    )


class RenderWorkerEnrollment(Base):
    __tablename__ = "render_worker_enrollments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("render_workers.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_render_worker_enrollments_status_expiry", "status", "expires_at"),)


class RenderExecution(Base):
    __tablename__ = "render_executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    render_job_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("render_jobs.id", ondelete="CASCADE"), nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("render_workers.id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fence_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="claimed")
    render_spec: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    capability_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    transfer_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="http")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    log_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("render_job_id", "attempt", name="uq_render_executions_job_attempt"),
        Index("ix_render_executions_worker_state", "worker_id", "state", "created_at"),
        Index("ix_render_executions_job_state", "render_job_id", "state"),
        Index("ix_render_executions_lease", "state", "lease_until"),
    )


class SubtitleJob(Base):
    __tablename__ = "subtitle_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)

    status: Mapped[SubtitleJobStatus] = mapped_column(Enum(SubtitleJobStatus, name="subtitle_job_status"), nullable=False, default=SubtitleJobStatus.queued)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0..100
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    logs_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_subtitle_jobs_task_status", "task_id", "status"),
        Index("ix_subtitle_jobs_status_created_at", "status", "created_at"),
        Index("ix_subtitle_jobs_status_lease_until", "status", "lease_until", "created_at"),
        Index("ix_subtitle_jobs_operation_key", "operation_key"),
    )


class YouTubeSourceType(str, enum.Enum):
    channel = "channel"
    playlist = "playlist"


class YouTubeSource(Base):
    __tablename__ = "youtube_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[YouTubeSourceType] = mapped_column(Enum(YouTubeSourceType, name="youtube_source_type"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    license: Mapped[SourceLicense] = mapped_column(Enum(SourceLicense, name="youtube_source_license"), nullable=False, default=SourceLicense.own)
    proof_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    scan_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    auto_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_scan_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scan_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scan_discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_scan_created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_scan_started_pipeline_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_scan_skipped_duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_scan_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scan_lock_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    scan_lock_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_youtube_sources_type_id"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    args_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_outbox_events_pending_lease", "status", "available_at", "lease_until"),
        Index("ix_outbox_events_operation_key", "operation_key"),
    )


class OperationInbox(Base):
    __tablename__ = "operation_inbox"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON_PAYLOAD, nullable=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_operation_inbox_operation_key"),
        Index("ix_operation_inbox_pending_lease", "status", "lease_until", "created_at"),
    )


class RemoteAPIRequest(Base):
    __tablename__ = "remote_api_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    response_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON_PAYLOAD, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("token_hash", "idempotency_key", name="uq_remote_api_requests_token_idempotency"),
        Index("ix_remote_api_requests_pending_lease", "status", "lease_until", "created_at"),
    )


class AIUsageEvent(Base):
    """One terminal OpenAI-compatible request attempt used for operations telemetry."""

    __tablename__ = "ai_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    operation: Mapped[str] = mapped_column(String(96), nullable=False, default="chat")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_microusd: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_ai_usage_events_created", "created_at"),
        Index("ix_ai_usage_events_model_created", "model", "created_at"),
        Index("ix_ai_usage_events_status_created", "status_code", "created_at"),
        Index("ix_ai_usage_events_task_created", "task_id", "created_at"),
    )


class AlertEvent(Base):
    """Deduplicated operational alert visible in the unified alert center."""

    __tablename__ = "alert_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_alert_events_fingerprint"),
        Index("ix_alert_events_status_severity_seen", "status", "severity", "last_seen_at"),
        Index("ix_alert_events_source_status", "source", "status"),
    )


class DesktopAccessGrant(Base):
    __tablename__ = "desktop_access_grants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    last_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_desktop_access_grants_token_hash"),
        Index("ix_desktop_access_grants_active_expiry", "status", "expires_at"),
    )


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON_PAYLOAD, nullable=False, default=dict)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_security_audit_events_type_created", "event_type", "created_at"),
        Index("ix_security_audit_events_actor_created", "actor_type", "actor_id", "created_at"),
    )


class IngestedVideo(Base):
    __tablename__ = "ingested_videos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube")
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)  # youtube videoId

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("platform", "source_id", name="uq_ingested_videos_platform_source_id"),
        Index("ix_ingested_videos_task", "task_id"),
    )


class YouTubeVideoMeta(Base):
    __tablename__ = "youtube_video_meta"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)

    source_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # youtube videoId

    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    webpage_url: Mapped[str] = mapped_column(Text, nullable=False, default="")

    uploader: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    upload_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # YYYYMMDD
    duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_youtube_video_meta_source_id", "source_id"),
    )
