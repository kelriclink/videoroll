"""Add queue ordering, AI usage telemetry and operational alerts.

Revision ID: 0005_operations_center
Revises: 0004_playout_asset_links
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from videoroll.db.schema_compat import add_column_if_missing, create_index_if_missing, create_table_if_missing


revision: str = "0005_operations_center"
down_revision: str | None = "0004_playout_asset_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_type() -> sa.TypeEngine:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    add_column_if_missing("tasks", sa.Column("queue_position", sa.BigInteger(), nullable=True))
    create_index_if_missing(
        "ix_tasks_queue_priority_position",
        "tasks",
        ["priority", "queue_position", "created_at"],
        unique=False,
    )

    create_table_if_missing(
        "ai_usage_events",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("task_id", _uuid_type(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False, server_default="openai"),
        sa.Column("model", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("operation", sa.String(length=96), nullable=False, server_default="chat"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("error_type", sa.String(length=96), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    create_index_if_missing("ix_ai_usage_events_created", "ai_usage_events", ["created_at"], unique=False)
    create_index_if_missing("ix_ai_usage_events_model_created", "ai_usage_events", ["model", "created_at"], unique=False)
    create_index_if_missing("ix_ai_usage_events_status_created", "ai_usage_events", ["status_code", "created_at"], unique=False)
    create_index_if_missing("ix_ai_usage_events_task_created", "ai_usage_events", ["task_id", "created_at"], unique=False)

    create_table_if_missing(
        "alert_events",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "details_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_alert_events_fingerprint"),
    )
    create_index_if_missing(
        "ix_alert_events_status_severity_seen",
        "alert_events",
        ["status", "severity", "last_seen_at"],
        unique=False,
    )
    create_index_if_missing("ix_alert_events_source_status", "alert_events", ["source", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_alert_events_source_status", table_name="alert_events")
    op.drop_index("ix_alert_events_status_severity_seen", table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_index("ix_ai_usage_events_task_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_status_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_model_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_created", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")
    op.drop_index("ix_tasks_queue_priority_position", table_name="tasks")
    op.drop_column("tasks", "queue_position")
