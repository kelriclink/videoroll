"""Frozen core schema for databases created before Alembic was introduced.

Keep this snapshot independent of live ORM models: later model changes must not
rewrite the starting schema of historical migrations. Existing tables and rows
are left intact; additive compatibility checks upgrade supported old columns.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection


def legacy_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table(
        "accounts", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("platform", sa.Enum("bilibili", "youtube", "douyin", "xiaohongshu", "kuaishou", "tencent", name="platform"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("secrets_encrypted", sa.Text(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("check_state", sa.String(length=16), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("platform", "name", name="uq_accounts_platform_name"),
    )
    sa.Table(
        "app_settings", metadata,
        sa.Column("key", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("value_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    sa.Table(
        "tasks", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("source_type", sa.Enum("youtube", "local", "url", name="source_type"), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_license", sa.Enum("own", "authorized", "cc", "unknown", name="source_license"), nullable=False),
        sa.Column("source_proof_url", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("created", "ingested", "downloaded", "audio_extracted", "asr_done", "translated", "subtitle_ready", "rendered", "ready_for_review", "approved", "publishing", "published", "failed", "canceled", name="task_status"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("active_publish_batch_id", sa.Uuid(), nullable=True),
        sa.Column("lock_owner", sa.String(length=128), nullable=True),
        sa.Column("lock_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_tasks_active_publish_batch_id", "active_publish_batch_id", unique=False),
        sa.Index("ix_tasks_lock_until", "lock_owner", "lock_until", unique=False),
        sa.Index("ix_tasks_status_created_at", "status", "created_at", unique=False),
    )
    sa.Table(
        "youtube_sources", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("source_type", sa.Enum("channel", "playlist", name="youtube_source_type"), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("license", sa.Enum("own", "authorized", "cc", "unknown", name="youtube_source_license"), nullable=False),
        sa.Column("proof_url", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("scan_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("scan_limit", sa.Integer(), nullable=False),
        sa.Column("auto_process", sa.Boolean(), nullable=False),
        sa.Column("last_scan_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_discovered_count", sa.Integer(), nullable=False),
        sa.Column("last_scan_created_count", sa.Integer(), nullable=False),
        sa.Column("last_scan_started_pipeline_count", sa.Integer(), nullable=False),
        sa.Column("last_scan_skipped_duplicates", sa.Integer(), nullable=False),
        sa.Column("last_scan_error", sa.Text(), nullable=True),
        sa.Column("scan_lock_owner", sa.String(length=128), nullable=True),
        sa.Column("scan_lock_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_type", "source_id", name="uq_youtube_sources_type_id"),
    )
    sa.Table(
        "assets", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Enum("video_raw", "metadata_json", "audio_wav", "segments_json", "subtitle_srt", "subtitle_ass", "video_final", "cover_image", "log", "publish_result", name="asset_kind"), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_assets_task_kind", "task_id", "kind", unique=False),
    )
    sa.Table(
        "ingested_videos", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("platform", "source_id", name="uq_ingested_videos_platform_source_id"),
        sa.Index("ix_ingested_videos_task", "task_id", unique=False),
    )
    sa.Table(
        "publish_batches", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("expected_targets", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("request_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("outcomes_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("cleanup_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_delivery_version", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_publish_batches_task_state", "task_id", "state", unique=False),
    )
    sa.Table(
        "subtitle_jobs", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Enum("queued", "running", "succeeded", "failed", name="subtitle_job_status"), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("request_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("logs_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_subtitle_jobs_status_created_at", "status", "created_at", unique=False),
        sa.Index("ix_subtitle_jobs_task_status", "task_id", "status", unique=False),
    )
    sa.Table(
        "subtitles", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("format", sa.Enum("srt", "vtt", "ass", name="subtitle_format"), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("editor", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_subtitles_task_version", "task_id", "version", unique=False),
    )
    sa.Table(
        "youtube_video_meta", metadata,
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("webpage_url", sa.Text(), nullable=False),
        sa.Column("uploader", sa.String(length=256), nullable=True),
        sa.Column("upload_date", sa.String(length=32), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_youtube_video_meta_source_id", "source_id", unique=False),
    )
    sa.Table(
        "publish_jobs", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("publish_batches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("platform", sa.Enum("bilibili", "youtube", "douyin", "xiaohongshu", "kuaishou", "tencent", name="platform"), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("bili_account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("meta_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("cover_key", sa.Text(), nullable=True),
        sa.Column("state", sa.Enum("draft", "submitting", "submitted", "published", "unknown", "failed", name="publish_state"), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("bvid", sa.String(length=32), nullable=True),
        sa.Column("aid", sa.String(length=32), nullable=True),
        sa.Column("response_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_publish_jobs_batch_platform_account", "batch_id", "platform", "account_id", unique=False),
        sa.Index("ix_publish_jobs_platform_state", "platform", "state", unique=False),
        sa.Index("ix_publish_jobs_task_state", "task_id", "state", unique=False),
    )
    sa.Table(
        "render_jobs", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subtitle_job_id", sa.Uuid(), sa.ForeignKey("subtitle_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.Enum("queued", "running", "succeeded", "failed", "canceled", name="render_job_status"), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("request_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_render_jobs_status_created_at", "status", "created_at", unique=False),
        sa.Index("ix_render_jobs_subtitle_job", "subtitle_job_id", unique=False),
        sa.Index("ix_render_jobs_task_status", "task_id", "status", unique=False),
    )
    return metadata


def create_legacy_tables(connection: Connection, *, offline: bool = False) -> None:
    legacy_metadata().create_all(connection, checkfirst=not offline)
