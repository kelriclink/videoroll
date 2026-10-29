"""Make workflow origin and download recovery state explicit.

Revision ID: 0013_execution_state
Revises: 0012_source_scan_stats
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from videoroll.db.schema_compat import add_column_if_missing, create_index_if_missing


revision = "0013_execution_state"
down_revision = "0012_source_scan_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("tasks", sa.Column("origin_kind", sa.String(32)))
    add_column_if_missing("tasks", sa.Column("origin_run_id", sa.String(64)))
    add_column_if_missing(
        "tasks",
        sa.Column("restart_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    create_index_if_missing("ix_tasks_origin_kind_status", "tasks", ["origin_kind", "status"])

    add_column_if_missing(
        "operation_inbox",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    add_column_if_missing("operation_inbox", sa.Column("retry_after", sa.DateTime(timezone=True)))
    add_column_if_missing(
        "operation_inbox",
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
    )
    add_column_if_missing("operation_inbox", sa.Column("restart_token", sa.String(128)))
    create_index_if_missing(
        "ix_operation_inbox_retry",
        "operation_inbox",
        ["status", "retry_after", "lease_until", "created_at"],
    )

    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE tasks SET origin_kind = 'auto_youtube' "
        "WHERE origin_kind IS NULL AND created_by LIKE 'auto_youtube;%'"
    ))
    bind.execute(sa.text(
        "UPDATE tasks SET origin_kind = 'youtube_home_scan' "
        "WHERE origin_kind IS NULL AND created_by LIKE 'youtube_home_scan;%'"
    ))
    bind.execute(sa.text(
        "UPDATE tasks SET origin_kind = 'youtube_task_restart', restart_generation = 1 "
        "WHERE origin_kind IS NULL AND created_by LIKE 'youtube_task_restart;%'"
    ))


def downgrade() -> None:
    op.drop_index("ix_operation_inbox_retry", table_name="operation_inbox")
    for name in ("restart_token", "recovery_count", "retry_after", "attempt_count"):
        op.drop_column("operation_inbox", name)
    op.drop_index("ix_tasks_origin_kind_status", table_name="tasks")
    for name in ("restart_generation", "origin_run_id", "origin_kind"):
        op.drop_column("tasks", name)
