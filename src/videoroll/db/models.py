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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from videoroll.db.base import Base


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
    failed = "failed"


class Platform(str, enum.Enum):
    bilibili = "bilibili"
    youtube = "youtube"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType, name="source_type"), nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source_license: Mapped[SourceLicense] = mapped_column(Enum(SourceLicense, name="source_license"), nullable=False)
    source_proof_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.created)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    lock_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lock_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    assets: Mapped[list["Asset"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    subtitles: Mapped[list["Subtitle"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    publish_jobs: Mapped[list["PublishJob"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_tasks_status_created_at", "status", "created_at"),
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

    bili_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)

    meta_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cover_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    state: Mapped[PublishState] = mapped_column(Enum(PublishState, name="publish_state"), nullable=False, default=PublishState.draft)
    bvid: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    aid: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    response_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    task: Mapped["Task"] = relationship(back_populates="publish_jobs")
    account: Mapped[Optional["Account"]] = relationship()

    __table_args__ = (
        Index("ix_publish_jobs_task_state", "task_id", "state"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[Platform] = mapped_column(Enum(Platform, name="platform"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    secrets_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("platform", "name", name="uq_accounts_platform_name"),
    )


class SubtitleJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class SubtitleJob(Base):
    __tablename__ = "subtitle_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)

    status: Mapped[SubtitleJobStatus] = mapped_column(Enum(SubtitleJobStatus, name="subtitle_job_status"), nullable=False, default=SubtitleJobStatus.queued)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0..100
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    logs_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_subtitle_jobs_task_status", "task_id", "status"),
    )


class YouTubeSourceType(str, enum.Enum):
    channel = "channel"
    playlist = "playlist"


class YouTubeSource(Base):
    __tablename__ = "youtube_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[YouTubeSourceType] = mapped_column(Enum(YouTubeSourceType, name="youtube_source_type"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)

    license: Mapped[SourceLicense] = mapped_column(Enum(SourceLicense, name="youtube_source_license"), nullable=False, default=SourceLicense.own)
    proof_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_youtube_sources_type_id"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


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
