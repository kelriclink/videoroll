from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from videoroll.db.session import get_engine
from videoroll.db.schema_lock import schema_lock


logger = logging.getLogger(__name__)
Bind = Engine | Connection


@contextmanager
def _connection(bind: Bind):
    if isinstance(bind, Connection):
        yield bind
    else:
        with bind.connect() as connection:
            yield connection


@contextmanager
def _transaction(bind: Bind):
    if isinstance(bind, Connection):
        transaction = bind.begin_nested() if bind.in_transaction() else bind.begin()
        with transaction:
            yield bind
    else:
        with bind.begin() as connection:
            yield connection


def _is_duplicate_column_error(exc: Exception) -> bool:
    """
    Best-effort detection across dialects/drivers.

    We want auto-migrations to be safe under concurrent startups where two
    processes may race to add the same column.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate == "42701":  # PostgreSQL duplicate_column
        return True

    msg = str(exc).lower()
    return ("duplicate column" in msg) or ("already exists" in msg and "column" in msg)


def _add_column(engine: Bind, table: str, column: str, column_type_sql: str) -> None:
    stmt = f"ALTER TABLE {table} ADD COLUMN {column} {column_type_sql}"
    try:
        with _transaction(engine) as conn:
            conn.execute(text(stmt))
    except Exception as e:
        if _is_duplicate_column_error(e):
            return
        raise


def _ensure_postgres_enum_values(engine: Bind) -> None:
    if (engine.dialect.name or "").lower() != "postgresql":
        return
    required = {
        "platform": ["douyin", "xiaohongshu", "kuaishou", "tencent"],
        "publish_state": ["unknown"],
    }
    with _transaction(engine) as conn:
        for enum_name, values in required.items():
            exists = conn.execute(
                text("SELECT 1 FROM pg_type WHERE typname = :enum_name"),
                {"enum_name": enum_name},
            ).scalar()
            if not exists:
                continue
            for value in values:
                conn.execute(text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'"))


def _ensure_tasks_stop_columns(engine: Bind) -> None:
    insp = inspect(engine)
    if "tasks" not in set(insp.get_table_names()):
        return

    cols = {c.get("name") for c in insp.get_columns("tasks")}
    if "stopped_status" in cols:
        return
    column_type = "task_status" if (engine.dialect.name or "").lower() == "postgresql" else "VARCHAR(32)"
    _add_column(engine, "tasks", "stopped_status", column_type)
    logger.warning("auto-migrated DB: added tasks.stopped_status")


def _ensure_app_settings_version_column(engine: Bind) -> None:
    insp = inspect(engine)
    if "app_settings" not in set(insp.get_table_names()):
        return

    columns = {column.get("name") for column in insp.get_columns("app_settings")}
    if "version" not in columns:
        _add_column(engine, "app_settings", "version", "INTEGER NOT NULL DEFAULT 1")
        logger.warning("auto-migrated DB: added app_settings.version")


def _ensure_job_lease_columns(engine: Bind) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    lease_until_type = "TIMESTAMPTZ" if (engine.dialect.name or "").lower() == "postgresql" else "TIMESTAMP"
    required_columns = {
        "lease_owner": "VARCHAR(128)",
        "lease_until": lease_until_type,
        "heartbeat_at": lease_until_type,
        "operation_key": "VARCHAR(255)",
    }

    for table_name in ("subtitle_jobs", "render_jobs", "publish_jobs"):
        if table_name not in tables:
            continue
        columns = {column.get("name") for column in insp.get_columns(table_name)}
        for column, column_type_sql in required_columns.items():
            if column not in columns:
                _add_column(engine, table_name, column, column_type_sql)
                logger.warning("auto-migrated DB: added %s.%s", table_name, column)

        status_column = "state" if "state" in columns else "status" if "status" in columns else None
        if status_column is None or "created_at" not in columns:
            continue
        with _transaction(engine) as conn:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table_name}_{status_column}_lease_until "
                    f"ON {table_name} ({status_column}, lease_until, created_at)"
                )
            )
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_operation_key ON {table_name} (operation_key)")
            )


def _ensure_tasks_publish_batch_columns(engine: Bind) -> None:
    insp = inspect(engine)
    if "tasks" not in set(insp.get_table_names()):
        return
    cols = {c.get("name") for c in insp.get_columns("tasks")}
    if "active_publish_batch_id" not in cols:
        _add_column(engine, "tasks", "active_publish_batch_id", "UUID")
        logger.warning("auto-migrated DB: added tasks.active_publish_batch_id")
    with _transaction(engine) as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_active_publish_batch_id ON tasks (active_publish_batch_id)"))


def _backfill_publish_batch_lifecycle(engine: Bind) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if "tasks" not in tables or "publish_batches" not in tables:
        return
    task_columns = {column.get("name") for column in insp.get_columns("tasks")}
    batch_columns = {column.get("name") for column in insp.get_columns("publish_batches")}
    if "active_publish_batch_id" not in task_columns or "cleanup_delivery_version" not in batch_columns:
        return
    with _transaction(engine) as conn:
        conn.execute(
            text(
                """
                UPDATE tasks
                SET active_publish_batch_id = (
                    SELECT publish_batches.id
                    FROM publish_batches
                    WHERE publish_batches.task_id = tasks.id
                    ORDER BY publish_batches.created_at DESC, publish_batches.id DESC
                    LIMIT 1
                )
                WHERE active_publish_batch_id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM publish_batches WHERE publish_batches.task_id = tasks.id
                  )
                """
            )
        )
        # Markers written before reliable broker delivery cannot prove that a
        # cleanup message was accepted.  Replay them once through the v2 outbox.
        conn.execute(
            text(
                """
                UPDATE publish_batches
                SET cleanup_enqueued_at = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM tasks
                            WHERE tasks.active_publish_batch_id = publish_batches.id
                        ) THEN NULL
                        ELSE cleanup_enqueued_at
                    END,
                    cleanup_delivery_version = 2
                WHERE cleanup_delivery_version < 2
                """
            )
        )


def _ensure_youtube_sources_columns(engine: Bind) -> None:
    insp = inspect(engine)
    if "youtube_sources" not in set(insp.get_table_names()):
        return

    cols = {c.get("name") for c in insp.get_columns("youtube_sources")}
    dialect = (engine.dialect.name or "").lower()
    ts_type = "TIMESTAMPTZ" if dialect == "postgresql" else "TIMESTAMP"

    required_columns = {
        "source_url": "TEXT",
        "display_name": "VARCHAR(255)",
        "scan_interval_minutes": "INTEGER DEFAULT 60",
        "scan_limit": "INTEGER DEFAULT 20",
        "auto_process": "BOOLEAN DEFAULT TRUE",
        "last_scan_started_at": ts_type,
        "last_scan_finished_at": ts_type,
        "last_scan_discovered_count": "INTEGER DEFAULT 0",
        "last_scan_created_count": "INTEGER DEFAULT 0",
        "last_scan_started_pipeline_count": "INTEGER DEFAULT 0",
        "last_scan_skipped_duplicates": "INTEGER DEFAULT 0",
        "last_scan_error": "TEXT",
        "scan_lock_owner": "VARCHAR(128)",
        "scan_lock_until": ts_type,
    }

    for column, column_type_sql in required_columns.items():
        if column in cols:
            continue
        _add_column(engine, "youtube_sources", column, column_type_sql)
        logger.warning("auto-migrated DB: added youtube_sources.%s", column)


def _ensure_scheduler_indexes(engine: Bind) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with _transaction(engine) as conn:
        if "subtitle_jobs" in tables:
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_subtitle_jobs_status_created_at ON subtitle_jobs (status, created_at)")
            )
        if "render_jobs" in tables:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_render_jobs_status_created_at ON render_jobs (status, created_at)"))


def _ensure_publish_jobs_generic_columns(engine: Bind) -> None:
    insp = inspect(engine)
    if "publish_jobs" not in set(insp.get_table_names()):
        return

    cols = {c.get("name") for c in insp.get_columns("publish_jobs")}
    dialect = (engine.dialect.name or "").lower()
    ts_type = "TIMESTAMPTZ" if dialect == "postgresql" else "TIMESTAMP"
    required_columns = {
        "batch_id": "UUID",
        "platform": "VARCHAR(32) DEFAULT 'bilibili' NOT NULL",
        "account_id": "UUID",
        "external_id": "VARCHAR(128)",
        "external_url": "TEXT",
        "started_at": ts_type,
        "finished_at": ts_type,
        "lease_owner": "VARCHAR(128)",
        "lease_until": ts_type,
        "heartbeat_at": ts_type,
        "operation_key": "VARCHAR(255)",
        "upload_progress": "INTEGER NOT NULL DEFAULT 0",
        "upload_active": "BOOLEAN NOT NULL DEFAULT FALSE",
    }
    for column, column_type_sql in required_columns.items():
        if column in cols:
            continue
        _add_column(engine, "publish_jobs", column, column_type_sql)
        logger.warning("auto-migrated DB: added publish_jobs.%s", column)

    with _transaction(engine) as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_publish_jobs_platform_state ON publish_jobs (platform, state)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_publish_jobs_batch_platform_account "
                "ON publish_jobs (batch_id, platform, account_id)"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_publish_jobs_operation_key "
                "ON publish_jobs (operation_key) WHERE operation_key IS NOT NULL"
            )
        )


def _backfill_bilibili_publish_account_ids(engine: Bind) -> None:
    insp = inspect(engine)
    if "publish_jobs" not in set(insp.get_table_names()):
        return
    cols = {column.get("name") for column in insp.get_columns("publish_jobs")}
    if not {"platform", "account_id", "bili_account_id"}.issubset(cols):
        return
    with _transaction(engine) as conn:
        conn.execute(
            text(
                """
                UPDATE publish_jobs
                SET account_id = bili_account_id
                WHERE platform = 'bilibili'
                  AND account_id IS NULL
                  AND bili_account_id IS NOT NULL
                """
            )
        )


def _ensure_publish_batch_columns(engine: Bind) -> None:
    insp = inspect(engine)
    if "publish_batches" not in set(insp.get_table_names()):
        return
    cols = {c.get("name") for c in insp.get_columns("publish_batches")}
    required_columns = {
        "request_json": "JSONB NOT NULL DEFAULT '{}'::jsonb" if (engine.dialect.name or "").lower() == "postgresql" else "JSON NOT NULL DEFAULT '{}'",
        "cleanup_delivery_version": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, column_type_sql in required_columns.items():
        if column not in cols:
            _add_column(engine, "publish_batches", column, column_type_sql)
            logger.warning("auto-migrated DB: added publish_batches.%s", column)


def _ensure_account_check_columns(engine: Bind) -> None:
    insp = inspect(engine)
    if "accounts" not in set(insp.get_table_names()):
        return
    cols = {c.get("name") for c in insp.get_columns("accounts")}
    dialect = (engine.dialect.name or "").lower()
    ts_type = "TIMESTAMPTZ" if dialect == "postgresql" else "TIMESTAMP"
    required_columns = {
        "check_state": "VARCHAR(16) DEFAULT 'unchecked' NOT NULL",
        "last_checked_at": ts_type,
        "last_check_message": "TEXT",
    }
    for column, column_type_sql in required_columns.items():
        if column in cols:
            continue
        _add_column(engine, "accounts", column, column_type_sql)
        logger.warning("auto-migrated DB: added accounts.%s", column)


def _ensure_pgvector_ann_indexes(conn) -> None:
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_translation_knowledge_vector_filter
            ON translation_knowledge_items (target_lang, embedding_model, status, domain)
            WHERE embedding IS NOT NULL
            """
        )
    )
    try:
        with conn.begin_nested():
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_translation_knowledge_embedding_hnsw
                    ON translation_knowledge_items
                    USING hnsw (embedding vector_cosine_ops)
                    WHERE embedding IS NOT NULL
                    """
                )
            )
    except Exception as e:
        logger.warning("pgvector HNSW index is unavailable; vector search will use exact scan: %s", e)



def _ensure_pgvector_rag_tables(engine: Bind) -> None:
    dialect = (engine.dialect.name or "").lower()
    if dialect != "postgresql":
        logger.warning("RAG tables require PostgreSQL/pgvector; skipping for dialect=%s", dialect)
        return

    with _connection(engine) as conn:
        vector_type = conn.execute(text("SELECT to_regtype('vector')")).scalar()
        if vector_type is None:
            try:
                with _transaction(engine) as ext_conn:
                    ext_conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as e:
                logger.warning("pgvector extension is unavailable; skipping RAG table migration: %s", e)
                return

    with _transaction(engine) as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS translation_knowledge_items (
                    id UUID PRIMARY KEY,
                    item_type VARCHAR(32) NOT NULL DEFAULT 'document',
                    term TEXT NOT NULL DEFAULT '',
                    normalized_term TEXT NOT NULL DEFAULT '',
                    translation TEXT NOT NULL DEFAULT '',
                    target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
                    domain TEXT NOT NULL DEFAULT '',
                    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    status VARCHAR(32) NOT NULL DEFAULT 'approved',
                    created_by VARCHAR(32) NOT NULL DEFAULT 'manual',
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    embedding vector,
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_text_hash VARCHAR(64) NOT NULL DEFAULT '',
                    last_verified_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        try:
            with conn.begin_nested():
                conn.execute(text("ALTER TABLE translation_knowledge_items ALTER COLUMN embedding TYPE vector"))
        except Exception:
            pass
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_term_evidence (
                    id UUID PRIMARY KEY,
                    term TEXT NOT NULL DEFAULT '',
                    normalized_term TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    results JSONB NOT NULL DEFAULT '[]'::jsonb,
                    summary TEXT NOT NULL DEFAULT '',
                    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_term_matches (
                    id UUID PRIMARY KEY,
                    task_id UUID,
                    subtitle_job_id UUID,
                    knowledge_item_id UUID REFERENCES translation_knowledge_items(id) ON DELETE SET NULL,
                    term TEXT NOT NULL DEFAULT '',
                    normalized_term TEXT NOT NULL DEFAULT '',
                    segment_start DOUBLE PRECISION,
                    segment_end DOUBLE PRECISION,
                    raw_context TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_agent_runs (
                    id UUID PRIMARY KEY,
                    agent_type VARCHAR(64) NOT NULL DEFAULT 'rag_term_research',
                    status VARCHAR(32) NOT NULL DEFAULT 'running',
                    term TEXT NOT NULL DEFAULT '',
                    normalized_term TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
                    task_id UUID,
                    subtitle_job_id UUID,
                    query TEXT NOT NULL DEFAULT '',
                    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
                    result JSONB NOT NULL DEFAULT '{}'::jsonb,
                    error TEXT NOT NULL DEFAULT '',
                    knowledge_item_id UUID REFERENCES translation_knowledge_items(id) ON DELETE SET NULL,
                    parent_agent_run_id UUID,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    finished_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE translation_agent_runs
                ADD COLUMN IF NOT EXISTS parent_agent_run_id UUID
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE translation_agent_runs
                ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE translation_agent_runs
                ADD COLUMN IF NOT EXISTS checkpoint_version INTEGER NOT NULL DEFAULT 0
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE translation_agent_runs
                ADD COLUMN IF NOT EXISTS checkpointed_at TIMESTAMPTZ
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE translation_agent_runs
                ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(128)
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE translation_agent_runs
                ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_agent_events (
                    id UUID PRIMARY KEY,
                    run_id UUID NOT NULL REFERENCES translation_agent_runs(id) ON DELETE CASCADE,
                    kind VARCHAR(32) NOT NULL DEFAULT 'agent',
                    action VARCHAR(128) NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL DEFAULT 'ok',
                    event JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_agent_events_run_created
                ON translation_agent_events (run_id, created_at)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_agent_events_kind_action
                ON translation_agent_events (kind, action)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_lease
                ON translation_agent_runs (status, lease_until)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_translation_terms_lang_domain_norm
                ON translation_knowledge_items (target_lang, domain, normalized_term)
                WHERE item_type = 'term' AND normalized_term <> ''
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_status_updated
                ON translation_agent_runs (status, updated_at DESC)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_parent
                ON translation_agent_runs (parent_agent_run_id, updated_at DESC)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_term
                ON translation_agent_runs (target_lang, domain, normalized_term)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_knowledge_type_status
                ON translation_knowledge_items (item_type, status, target_lang)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_knowledge_domain_norm
                ON translation_knowledge_items (domain, normalized_term)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_matches_task
                ON translation_term_matches (task_id, subtitle_job_id)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_memory_entries (
                    id UUID PRIMARY KEY,
                    source_text TEXT NOT NULL,
                    source_norm TEXT NOT NULL,
                    target_text TEXT NOT NULL,
                    target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
                    domain TEXT NOT NULL DEFAULT '',
                    task_id UUID,
                    subtitle_job_id UUID,
                    source_kind VARCHAR(32) NOT NULL DEFAULT 'machine',
                    status VARCHAR(32) NOT NULL DEFAULT 'machine',
                    quality_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_memory_target_task
                ON translation_memory_entries (target_lang, task_id, updated_at DESC)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_memory_target_status
                ON translation_memory_entries (target_lang, status, updated_at DESC)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_memory_source_norm
                ON translation_memory_entries (source_norm)
                """
            )
        )
        _ensure_pgvector_ann_indexes(conn)
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_dictionary_sources (
                    id UUID PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    slug TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    source_lang VARCHAR(16) NOT NULL DEFAULT '',
                    target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
                    format VARCHAR(32) NOT NULL DEFAULT 'csv',
                    license TEXT NOT NULL DEFAULT '',
                    license_url TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '',
                    attribution TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 0,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    entry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_dictionary_import_batches (
                    id UUID PRIMARY KEY,
                    source_id UUID REFERENCES translation_dictionary_sources(id) ON DELETE CASCADE,
                    status VARCHAR(32) NOT NULL DEFAULT 'running',
                    filename TEXT NOT NULL DEFAULT '',
                    archive_path TEXT NOT NULL DEFAULT '',
                    file_sha256 VARCHAR(64) NOT NULL DEFAULT '',
                    file_size_bytes BIGINT NOT NULL DEFAULT 0,
                    format VARCHAR(32) NOT NULL DEFAULT 'csv',
                    import_mode VARCHAR(32) NOT NULL DEFAULT 'upsert',
                    requested_by VARCHAR(64) NOT NULL DEFAULT 'manual',
                    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
                    error TEXT NOT NULL DEFAULT '',
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS translation_dictionary_entries (
                    id UUID PRIMARY KEY,
                    source_id UUID NOT NULL REFERENCES translation_dictionary_sources(id) ON DELETE CASCADE,
                    batch_id UUID REFERENCES translation_dictionary_import_batches(id) ON DELETE SET NULL,
                    source_lang VARCHAR(16) NOT NULL DEFAULT '',
                    target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
                    term TEXT NOT NULL DEFAULT '',
                    normalized_term TEXT NOT NULL DEFAULT '',
                    translations JSONB NOT NULL DEFAULT '[]'::jsonb,
                    translation_text TEXT NOT NULL DEFAULT '',
                    pos TEXT NOT NULL DEFAULT '',
                    definition TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
                    examples JSONB NOT NULL DEFAULT '[]'::jsonb,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    quality DOUBLE PRECISION NOT NULL DEFAULT 0,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    last_lookup_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_translation_dictionary_sources_slug
                ON translation_dictionary_sources (slug)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_translation_dictionary_entry_norm
                ON translation_dictionary_entries (source_id, source_lang, target_lang, normalized_term, domain)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_dictionary_entries_lookup
                ON translation_dictionary_entries (target_lang, source_lang, normalized_term)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_dictionary_entries_source_enabled
                ON translation_dictionary_entries (source_id, enabled, updated_at DESC)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_translation_dictionary_sources_enabled_priority
                ON translation_dictionary_sources (enabled, priority DESC, updated_at DESC)
                """
            )
        )


def apply_legacy_migrations(connection: Connection) -> None:
    """Run historical additive checks inside the caller's schema transaction.

    The unified initializer already holds the schema lock. This deliberately
    does not obtain another pooled connection or recursively acquire that lock.
    """
    _ensure_app_settings_version_column(connection)
    _ensure_job_lease_columns(connection)
    _ensure_tasks_stop_columns(connection)
    _ensure_tasks_publish_batch_columns(connection)
    _ensure_youtube_sources_columns(connection)
    _ensure_publish_jobs_generic_columns(connection)
    _backfill_bilibili_publish_account_ids(connection)
    _ensure_publish_batch_columns(connection)
    _backfill_publish_batch_lifecycle(connection)
    _ensure_account_check_columns(connection)
    _ensure_scheduler_indexes(connection)
    _ensure_pgvector_rag_tables(connection)


def auto_migrate_engine(engine: Engine) -> None:
    """Compatibility API for old callers; service startup uses initialize_database."""
    with engine.connect() as connection, schema_lock(connection):
        # Commit added PostgreSQL enum labels before later statements use them.
        _ensure_postgres_enum_values(connection)
        with connection.begin():
            apply_legacy_migrations(connection)


@lru_cache
def _auto_migrate_cached(database_url: str, pid: int) -> None:
    """
    Idempotent auto migration cached per process.
    """
    engine = get_engine(database_url)
    auto_migrate_engine(engine)


def auto_migrate(database_url: str, *, force: bool = False) -> None:
    if force:
        engine = get_engine(database_url)
        auto_migrate_engine(engine)
        return
    _auto_migrate_cached(database_url, os.getpid())
