from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from videoroll.db.session import get_engine


logger = logging.getLogger(__name__)
_AUTO_MIGRATE_ADVISORY_LOCK_KEY = 0x564944454F524F4C


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


def _add_column(engine: Engine, table: str, column: str, column_type_sql: str) -> None:
    stmt = f"ALTER TABLE {table} ADD COLUMN {column} {column_type_sql}"
    with engine.begin() as conn:
        try:
            conn.execute(text(stmt))
        except Exception as e:
            if _is_duplicate_column_error(e):
                return
            raise


def _ensure_tasks_lock_columns(engine: Engine) -> None:
    insp = inspect(engine)
    if "tasks" not in set(insp.get_table_names()):
        return

    cols = {c.get("name") for c in insp.get_columns("tasks")}
    dialect = (engine.dialect.name or "").lower()

    if "lock_owner" not in cols:
        _add_column(engine, "tasks", "lock_owner", "VARCHAR(128)")
        logger.warning("auto-migrated DB: added tasks.lock_owner")

    if "lock_until" not in cols:
        ts_type = "TIMESTAMPTZ" if dialect == "postgresql" else "TIMESTAMP"
        _add_column(engine, "tasks", "lock_until", ts_type)
        logger.warning("auto-migrated DB: added tasks.lock_until")


def _ensure_youtube_sources_columns(engine: Engine) -> None:
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


def _ensure_scheduler_indexes(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        if "tasks" in tables:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_lock_until ON tasks (lock_owner, lock_until)"))
        if "subtitle_jobs" in tables:
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_subtitle_jobs_status_created_at ON subtitle_jobs (status, created_at)")
            )
        if "render_jobs" in tables:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_render_jobs_status_created_at ON render_jobs (status, created_at)"))


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


@contextmanager
def _auto_migrate_lock(engine: Engine):
    if (engine.dialect.name or "").lower() != "postgresql":
        yield
        return
    conn = engine.connect()
    try:
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _AUTO_MIGRATE_ADVISORY_LOCK_KEY})
        conn.commit()
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _AUTO_MIGRATE_ADVISORY_LOCK_KEY})
            conn.commit()
        finally:
            conn.close()


def _ensure_pgvector_rag_tables(engine: Engine) -> None:
    dialect = (engine.dialect.name or "").lower()
    if dialect != "postgresql":
        logger.warning("RAG tables require PostgreSQL/pgvector; skipping for dialect=%s", dialect)
        return

    with engine.connect() as conn:
        vector_type = conn.execute(text("SELECT to_regtype('vector')")).scalar()
        try:
            conn.commit()
        except Exception:
            pass
        if vector_type is None:
            try:
                with engine.begin() as ext_conn:
                    ext_conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as e:
                logger.warning("pgvector extension is unavailable; skipping RAG table migration: %s", e)
                return

    with engine.begin() as conn:
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


def auto_migrate_engine(engine: Engine) -> None:
    """
    Small, targeted "auto migration" for deployments without Alembic.

    Today it ensures columns required by the task-level queue and youtube
    source subscriptions exist.
    """
    with _auto_migrate_lock(engine):
        _ensure_tasks_lock_columns(engine)
        _ensure_youtube_sources_columns(engine)
        _ensure_scheduler_indexes(engine)
        _ensure_pgvector_rag_tables(engine)


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
