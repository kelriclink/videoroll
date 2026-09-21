"""Add distributed render workers and execution attempts.

Revision ID: 0007_render_workers
Revises: 0006_agent_runtime
Create Date: 2026-09-21
"""
from __future__ import annotations
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from videoroll.db.schema_compat import create_index_if_missing, create_table_if_missing

revision: str = "0007_render_workers"
down_revision: str | None = "0006_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_PAYLOAD = sa.JSON().with_variant(JSONB(), "postgresql")

def upgrade() -> None:
    create_table_if_missing(
        "render_workers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("worker_key", sa.String(128), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("architecture", sa.String(32)),
        sa.Column("version", sa.String(64), nullable=False, server_default=""),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("render_spec_versions", JSON_PAYLOAD, nullable=False, server_default=sa.text("'[1]'")),
        sa.Column("capabilities", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("resources", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("labels", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(24), nullable=False, server_default="online"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("draining", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    create_index_if_missing("ix_render_workers_status_seen", "render_workers", ["status", "last_seen_at"])
    create_index_if_missing("ix_render_workers_enabled_draining", "render_workers", ["enabled", "draining"])
    create_table_if_missing(
        "render_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("render_job_id", sa.Uuid(), sa.ForeignKey("render_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("render_workers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fence_token", sa.String(64), nullable=False, unique=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="claimed"),
        sa.Column("render_spec", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("capability_snapshot", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("transfer_mode", sa.String(24), nullable=False, server_default="http"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("log_tail", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("output_json", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("render_job_id", "attempt", name="uq_render_executions_job_attempt"),
    )
    create_index_if_missing("ix_render_executions_worker_state", "render_executions", ["worker_id", "state", "created_at"])
    create_index_if_missing("ix_render_executions_job_state", "render_executions", ["render_job_id", "state"])
    create_index_if_missing("ix_render_executions_lease", "render_executions", ["state", "lease_until"])

def downgrade() -> None:
    op.drop_table("render_executions")
    op.drop_table("render_workers")
