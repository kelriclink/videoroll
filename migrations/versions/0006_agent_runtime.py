"""Add durable agent checkpoints and append-only trace events.

Revision ID: 0006_agent_runtime
Revises: 0005_operations_center
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from videoroll.db.schema_compat import add_column_if_missing, create_index_if_missing, create_table_if_missing


revision: str = "0006_agent_runtime"
down_revision: str | None = "0005_operations_center"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_type() -> sa.TypeEngine:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    # The agent trace tables historically came from the runtime
    # auto-migration path rather than Alembic. Fresh databases and legacy
    # databases upgraded only through Alembic therefore may not have the base
    # run table yet. Create the compatible base shape first, then use
    # add_column_if_missing below to upgrade installations where the table was
    # already created by auto_migrate.
    # The knowledge table predates Alembic. Revision 0006 is the first
    # versioned migration that references it, so create its historical base
    # shape here as well. Existing deployments are validated by
    # create_table_if_missing; fresh/offline upgrades get the full shape needed
    # by the runtime compatibility indexes that run after Alembic reaches head.
    create_table_if_missing(
        "translation_knowledge_items",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False, server_default="document"),
        sa.Column("term", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_term", sa.Text(), nullable=False, server_default=""),
        sa.Column("translation", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_lang", sa.String(length=16), nullable=False, server_default="zh"),
        sa.Column("domain", sa.Text(), nullable=False, server_default=""),
        sa.Column("aliases", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sources", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="approved"),
        sa.Column("created_by", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=False, server_default=""),
        sa.Column("embedding_text_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )

    create_table_if_missing(
        "translation_agent_runs",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("agent_type", sa.String(length=64), nullable=False, server_default="rag_term_research"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("term", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_term", sa.Text(), nullable=False, server_default=""),
        sa.Column("domain", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_lang", sa.String(length=16), nullable=False, server_default="zh"),
        sa.Column("task_id", _uuid_type(), nullable=True),
        sa.Column("subtitle_job_id", _uuid_type(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "steps",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "result",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "knowledge_item_id",
            _uuid_type(),
            sa.ForeignKey("translation_knowledge_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("parent_agent_run_id", _uuid_type(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )

    add_column_if_missing(
        "translation_agent_runs",
        sa.Column(
            "checkpoint",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    add_column_if_missing(
        "translation_agent_runs",
        sa.Column("checkpoint_version", sa.Integer(), nullable=False, server_default="0"),
    )
    add_column_if_missing(
        "translation_agent_runs",
        sa.Column("checkpointed_at", sa.DateTime(timezone=True), nullable=True),
    )
    add_column_if_missing(
        "translation_agent_runs",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    add_column_if_missing(
        "translation_agent_runs",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )

    create_table_if_missing(
        "translation_agent_events",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column(
            "run_id",
            _uuid_type(),
            sa.ForeignKey("translation_agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="agent"),
        sa.Column("action", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column(
            "event",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    create_index_if_missing(
        "ix_translation_agent_events_run_created",
        "translation_agent_events",
        ["run_id", "created_at"],
        unique=False,
    )
    create_index_if_missing(
        "ix_translation_agent_events_kind_action",
        "translation_agent_events",
        ["kind", "action"],
        unique=False,
    )
    create_index_if_missing(
        "ix_translation_agent_runs_lease",
        "translation_agent_runs",
        ["status", "lease_until"],
        unique=False,
    )
    create_index_if_missing(
        "ix_translation_agent_runs_status_updated",
        "translation_agent_runs",
        ["status", "updated_at"],
        unique=False,
    )
    create_index_if_missing(
        "ix_translation_agent_runs_parent",
        "translation_agent_runs",
        ["parent_agent_run_id", "updated_at"],
        unique=False,
    )
    create_index_if_missing(
        "ix_translation_agent_runs_term",
        "translation_agent_runs",
        ["target_lang", "domain", "normalized_term"],
        unique=False,
    )
    create_table_if_missing(
        "translation_memory_entries",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_norm", sa.Text(), nullable=False),
        sa.Column("target_text", sa.Text(), nullable=False),
        sa.Column("target_lang", sa.String(length=16), nullable=False, server_default="zh"),
        sa.Column("domain", sa.Text(), nullable=False, server_default=""),
        sa.Column("task_id", _uuid_type(), nullable=True),
        sa.Column("subtitle_job_id", _uuid_type(), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False, server_default="machine"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="machine"),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    create_index_if_missing(
        "ix_translation_memory_target_task",
        "translation_memory_entries",
        ["target_lang", "task_id", "updated_at"],
        unique=False,
    )
    create_index_if_missing(
        "ix_translation_memory_target_status",
        "translation_memory_entries",
        ["target_lang", "status", "updated_at"],
        unique=False,
    )
    create_index_if_missing(
        "ix_translation_memory_source_norm",
        "translation_memory_entries",
        ["source_norm"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_translation_memory_source_norm", table_name="translation_memory_entries")
    op.drop_index("ix_translation_memory_target_status", table_name="translation_memory_entries")
    op.drop_index("ix_translation_memory_target_task", table_name="translation_memory_entries")
    op.drop_table("translation_memory_entries")
    op.drop_index("ix_translation_agent_runs_term", table_name="translation_agent_runs")
    op.drop_index("ix_translation_agent_runs_parent", table_name="translation_agent_runs")
    op.drop_index("ix_translation_agent_runs_status_updated", table_name="translation_agent_runs")
    op.drop_index("ix_translation_agent_runs_lease", table_name="translation_agent_runs")
    op.drop_index("ix_translation_agent_events_kind_action", table_name="translation_agent_events")
    op.drop_index("ix_translation_agent_events_run_created", table_name="translation_agent_events")
    op.drop_table("translation_agent_events")
    op.drop_column("translation_agent_runs", "lease_until")
    op.drop_column("translation_agent_runs", "lease_owner")
    op.drop_column("translation_agent_runs", "checkpointed_at")
    op.drop_column("translation_agent_runs", "checkpoint_version")
    op.drop_column("translation_agent_runs", "checkpoint")
