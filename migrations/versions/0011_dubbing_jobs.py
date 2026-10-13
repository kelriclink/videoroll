"""Durable Chinese dubbing jobs and separate audio/P2 asset kinds.

Revision ID: 0011_dubbing_jobs
Revises: 0010_remove_legacy_task_queue
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from videoroll.db.schema_compat import create_index_if_missing, create_table_if_missing


revision = "0011_dubbing_jobs"
down_revision = "0010_remove_legacy_task_queue"
branch_labels = None
depends_on = None

PAYLOAD = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The existing Asset.kind column uses a PostgreSQL named enum.
        for name in ("dubbing_audio", "video_dubbed"):
            bind.execute(sa.text(f"ALTER TYPE asset_kind ADD VALUE IF NOT EXISTS '{name}'"))
    create_table_if_missing(
        "dubbing_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subtitle_job_id", sa.Uuid(), sa.ForeignKey("subtitle_jobs.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_json", PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("output_json", PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text()),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("operation_key", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "operation_key", name="uq_dubbing_jobs_task_operation"),
    )
    create_index_if_missing("ix_dubbing_jobs_status_lease", "dubbing_jobs", ["status", "lease_until", "created_at"])
    create_index_if_missing("ix_dubbing_jobs_task_created", "dubbing_jobs", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_table("dubbing_jobs")
    # Enum values are deliberately retained: PostgreSQL cannot safely remove
    # individual enum values when historical assets may still reference them.
