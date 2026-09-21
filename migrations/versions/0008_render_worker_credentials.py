"""Add per-worker credentials and one-time enrollment tokens.

Revision ID: 0008_render_worker_credentials
Revises: 0007_render_workers
Create Date: 2026-09-21
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision = "0008_render_worker_credentials"
down_revision = "0007_render_workers"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("render_workers", sa.Column("credential_hash", sa.String(64), nullable=True))
    op.add_column("render_workers", sa.Column("credential_issued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("render_workers", sa.Column("credential_revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_render_workers_credential_hash", "render_workers", ["credential_hash"])
    op.create_table(
        "render_worker_enrollments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("render_workers.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_render_worker_enrollments_status_expiry", "render_worker_enrollments", ["status", "expires_at"])

def downgrade() -> None:
    op.drop_table("render_worker_enrollments")
    op.drop_constraint("uq_render_workers_credential_hash", "render_workers", type_="unique")
    op.drop_column("render_workers", "credential_revoked_at")
    op.drop_column("render_workers", "credential_issued_at")
    op.drop_column("render_workers", "credential_hash")
