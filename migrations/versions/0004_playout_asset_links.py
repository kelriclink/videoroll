"""Map VideoRoll final assets to server-side ffplayout media files.

Revision ID: 0004_playout_asset_links
Revises: 0003_task_stop_controls
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from videoroll.db.schema_compat import create_index_if_missing, create_table_if_missing


revision: str = "0004_playout_asset_links"
down_revision: str | None = "0003_task_stop_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_type() -> sa.TypeEngine:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    create_table_if_missing(
        "playout_asset_links",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("task_id", _uuid_type(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", _uuid_type(), sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ffplayout_channel_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("relative_media_path", sa.Text(), nullable=False),
        sa.Column("transfer_mode", sa.String(length=16), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", name="uq_playout_asset_links_asset_id"),
        sa.UniqueConstraint("ffplayout_channel_id", "relative_media_path", name="uq_playout_asset_links_channel_path"),
    )
    create_index_if_missing("ix_playout_asset_links_task", "playout_asset_links", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_playout_asset_links_task", table_name="playout_asset_links")
    op.drop_table("playout_asset_links")
