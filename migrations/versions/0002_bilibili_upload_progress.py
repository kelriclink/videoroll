"""Persist Bilibili video upload progress for the web UI.

Revision ID: 0002_bilibili_upload_progress
Revises: 0001_security_architecture
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_bilibili_upload_progress"
down_revision: str | None = "0001_security_architecture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "publish_jobs",
        sa.Column("upload_progress", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "publish_jobs",
        sa.Column("upload_active", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("publish_jobs", "upload_active")
    op.drop_column("publish_jobs", "upload_progress")
