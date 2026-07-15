"""Retain the workflow stage when a user stops a task.

Revision ID: 0003_task_stop_controls
Revises: 0002_bilibili_upload_progress
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003_task_stop_controls"
down_revision: str | None = "0002_bilibili_upload_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("stopped_status", postgresql.ENUM(name="task_status", create_type=False), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "stopped_status")
