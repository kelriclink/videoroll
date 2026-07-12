from __future__ import annotations

from sqlalchemy import create_engine, text

from videoroll.db import auto_migrate
from videoroll.db.auto_migrate import _backfill_publish_batch_lifecycle


def test_publish_batch_lifecycle_migration_backfills_current_batch_and_replays_old_cleanup_once() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tasks (id TEXT PRIMARY KEY, active_publish_batch_id TEXT)"))
        conn.execute(
            text(
                """
                CREATE TABLE publish_batches (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cleanup_enqueued_at TEXT,
                    cleanup_delivery_version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        conn.execute(text("INSERT INTO tasks (id, active_publish_batch_id) VALUES ('task-1', NULL)"))
        conn.execute(
            text(
                """
                INSERT INTO publish_batches (id, task_id, state, created_at, cleanup_enqueued_at, cleanup_delivery_version)
                VALUES ('batch-old', 'task-1', 'succeeded', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 0)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO publish_batches (id, task_id, state, created_at, cleanup_enqueued_at, cleanup_delivery_version)
                VALUES ('batch-current', 'task-1', 'succeeded', '2026-01-02T00:00:00Z', '2026-01-02T01:00:00Z', 0)
                """
            )
        )

    _backfill_publish_batch_lifecycle(engine)

    with engine.connect() as conn:
        task = conn.execute(text("SELECT active_publish_batch_id FROM tasks WHERE id = 'task-1'")).scalar_one()
        old_batch = conn.execute(
            text("SELECT cleanup_enqueued_at, cleanup_delivery_version FROM publish_batches WHERE id = 'batch-old'")
        ).one()
        current_batch = conn.execute(
            text("SELECT cleanup_enqueued_at, cleanup_delivery_version FROM publish_batches WHERE id = 'batch-current'")
        ).one()

    assert task == "batch-current"
    assert old_batch == ("2026-01-01T01:00:00Z", 2)
    assert current_batch == (None, 2)


def test_bilibili_account_migration_backfills_generic_account_id() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE publish_jobs (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account_id TEXT,
                    bili_account_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO publish_jobs (id, platform, account_id, bili_account_id)
                VALUES ('job-1', 'bilibili', NULL, 'account-1')
                """
            )
        )

    assert hasattr(auto_migrate, "_backfill_bilibili_publish_account_ids")
    auto_migrate._backfill_bilibili_publish_account_ids(engine)

    with engine.connect() as conn:
        account_id = conn.execute(text("SELECT account_id FROM publish_jobs WHERE id = 'job-1'")).scalar_one()

    assert account_id == "account-1"
