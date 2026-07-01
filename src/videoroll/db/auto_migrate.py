from __future__ import annotations

import logging
import os
from functools import lru_cache

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from videoroll.db.session import get_engine


logger = logging.getLogger(__name__)


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


def auto_migrate_engine(engine: Engine) -> None:
    """
    Small, targeted "auto migration" for deployments without Alembic.

    Today it ensures columns required by the task-level queue and youtube
    source subscriptions exist.
    """
    _ensure_tasks_lock_columns(engine)
    _ensure_youtube_sources_columns(engine)
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
