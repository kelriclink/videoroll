"""Introduce versioned security architecture tables and lease columns.

Revision ID: 0001_security_architecture
Revises:
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_security_architecture"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _uuid_type() -> sa.TypeEngine:
    return sa.Uuid(as_uuid=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _lease_columns() -> list[sa.Column]:
    return [
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("task_name", sa.String(length=255), nullable=False),
        sa.Column("args_json", _json_type(), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_lease_columns(),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_pending_lease",
        "outbox_events",
        ["status", "available_at", "lease_until"],
        unique=False,
    )
    op.create_index("ix_outbox_events_operation_key", "outbox_events", ["operation_key"], unique=False)

    op.create_table(
        "operation_inbox",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("request_json", _json_type(), nullable=False),
        sa.Column("result_json", _json_type(), nullable=True),
        *_lease_columns(),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_key", name="uq_operation_inbox_operation_key"),
    )
    op.create_index(
        "ix_operation_inbox_pending_lease",
        "operation_inbox",
        ["status", "lease_until", "created_at"],
        unique=False,
    )

    op.create_table(
        "remote_api_requests",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_json", _json_type(), nullable=False),
        sa.Column("response_json", _json_type(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        *_lease_columns(),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_hash",
            "idempotency_key",
            name="uq_remote_api_requests_token_idempotency",
        ),
    )
    op.create_index(
        "ix_remote_api_requests_pending_lease",
        "remote_api_requests",
        ["status", "lease_until", "created_at"],
        unique=False,
    )

    op.create_table(
        "desktop_access_grants",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("scope_json", _json_type(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_desktop_access_grants_token_hash"),
    )
    op.create_index(
        "ix_desktop_access_grants_active_expiry",
        "desktop_access_grants",
        ["status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "security_audit_events",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("payload_json", _json_type(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_security_audit_events_type_created",
        "security_audit_events",
        ["event_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_security_audit_events_actor_created",
        "security_audit_events",
        ["actor_type", "actor_id", "created_at"],
        unique=False,
    )

    for table_name, status_column in (
        ("subtitle_jobs", "status"),
        ("render_jobs", "status"),
        ("publish_jobs", "state"),
    ):
        op.add_column(table_name, sa.Column("lease_owner", sa.String(length=128), nullable=True))
        op.add_column(table_name, sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table_name, sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table_name, sa.Column("operation_key", sa.String(length=255), nullable=True))
        op.create_index(
            f"ix_{table_name}_{status_column}_lease_until",
            table_name,
            [status_column, "lease_until", "created_at"],
            unique=False,
        )
        op.create_index(f"ix_{table_name}_operation_key", table_name, ["operation_key"], unique=False)

    op.add_column(
        "app_settings",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "version")

    for table_name, status_column in reversed(
        (
            ("subtitle_jobs", "status"),
            ("render_jobs", "status"),
            ("publish_jobs", "state"),
        )
    ):
        op.drop_index(f"ix_{table_name}_operation_key", table_name=table_name)
        op.drop_index(f"ix_{table_name}_{status_column}_lease_until", table_name=table_name)
        op.drop_column(table_name, "operation_key")
        op.drop_column(table_name, "heartbeat_at")
        op.drop_column(table_name, "lease_until")
        op.drop_column(table_name, "lease_owner")

    op.drop_index("ix_security_audit_events_actor_created", table_name="security_audit_events")
    op.drop_index("ix_security_audit_events_type_created", table_name="security_audit_events")
    op.drop_table("security_audit_events")
    op.drop_index("ix_desktop_access_grants_active_expiry", table_name="desktop_access_grants")
    op.drop_table("desktop_access_grants")
    op.drop_index("ix_remote_api_requests_pending_lease", table_name="remote_api_requests")
    op.drop_table("remote_api_requests")
    op.drop_index("ix_operation_inbox_pending_lease", table_name="operation_inbox")
    op.drop_table("operation_inbox")
    op.drop_index("ix_outbox_events_operation_key", table_name="outbox_events")
    op.drop_index("ix_outbox_events_pending_lease", table_name="outbox_events")
    op.drop_table("outbox_events")
