"""Preserve non-empty YouTube scan snapshots and post-upgrade counters.

Revision ID: 0012_source_scan_stats
Revises: 0011_dubbing_jobs
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from videoroll.db.schema_compat import add_column_if_missing

revision = "0012_source_scan_stats"
down_revision = "0011_dubbing_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "youtube_sources",
        sa.Column("last_nonempty_scan_finished_at", sa.DateTime(timezone=True)),
    )
    for name in (
        "last_nonempty_discovered_count",
        "total_scan_count",
        "total_failed_scan_count",
        "total_created_count",
        "total_started_pipeline_count",
    ):
        add_column_if_missing(
            "youtube_sources",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    # Older schema variants receive the prior non-empty snapshot lazily when scanned.


def downgrade() -> None:
    for name in (
        "total_started_pipeline_count", "total_created_count", "total_failed_scan_count",
        "total_scan_count", "last_nonempty_discovered_count", "last_nonempty_scan_finished_at",
    ):
        op.drop_column("youtube_sources", name)
