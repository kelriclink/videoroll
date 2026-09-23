"""Add pipeline run correlation history for workflow-engine migration.

Revision ID: 0009_pipeline_runs
Revises: 0008_render_worker_credentials
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from videoroll.db.schema_compat import create_index_if_missing, create_table_if_missing


revision = "0009_pipeline_runs"
down_revision = "0008_render_worker_credentials"
branch_labels = None
depends_on = None

JSON_PAYLOAD = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    create_table_if_missing(
        "pipeline_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("engine", sa.String(16), nullable=False),
        sa.Column("workflow_name", sa.String(128), nullable=False),
        sa.Column("external_run_id", sa.String(255)),
        sa.Column("state", sa.String(32), nullable=False, server_default="submitting"),
        sa.Column("request_json", JSON_PAYLOAD, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("engine", "external_run_id", name="uq_pipeline_runs_engine_external_run"),
    )
    create_index_if_missing("ix_pipeline_runs_task_created", "pipeline_runs", ["task_id", "created_at"])
    create_index_if_missing("ix_pipeline_runs_state_created", "pipeline_runs", ["state", "created_at"])


def downgrade() -> None:
    op.drop_table("pipeline_runs")
