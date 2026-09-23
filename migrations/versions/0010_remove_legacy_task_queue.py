"""Remove legacy task-level queue scheduler fields after Hatchet cutover.

Revision ID: 0010_remove_legacy_task_queue
Revises: 0009_pipeline_runs
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision = "0010_remove_legacy_task_queue"
down_revision = "0009_pipeline_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        op.drop_index("ix_tasks_queue_priority_position", table_name="tasks")
        op.drop_index("ix_tasks_lock_until", table_name="tasks")
        op.drop_column("tasks", "queue_position")
        op.drop_column("tasks", "lock_owner")
        op.drop_column("tasks", "lock_until")
        op.create_index("ix_tasks_priority_created_at", "tasks", ["priority", "created_at"], unique=False)
        return

    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    indexes = {index["name"] for index in inspector.get_indexes("tasks")}
    if "ix_tasks_queue_priority_position" in indexes:
        op.drop_index("ix_tasks_queue_priority_position", table_name="tasks")
    if "ix_tasks_lock_until" in indexes:
        op.drop_index("ix_tasks_lock_until", table_name="tasks")
    for column_name in ("queue_position", "lock_owner", "lock_until"):
        if column_name in columns:
            op.drop_column("tasks", column_name)
    if "ix_tasks_priority_created_at" not in indexes:
        op.create_index("ix_tasks_priority_created_at", "tasks", ["priority", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tasks_priority_created_at", table_name="tasks")
    op.add_column("tasks", sa.Column("lock_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("lock_owner", sa.String(length=128), nullable=True))
    op.add_column("tasks", sa.Column("queue_position", sa.BigInteger(), nullable=True))
    op.create_index("ix_tasks_lock_until", "tasks", ["lock_owner", "lock_until"], unique=False)
    op.create_index(
        "ix_tasks_queue_priority_position",
        "tasks",
        ["priority", "queue_position", "created_at"],
        unique=False,
    )
