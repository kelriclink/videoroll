-- Keep the original Python RAG agent history tables as the only durable
-- source for the V1 agent-run read API. Older installations created these
-- through Python auto_migrate rather than Alembic, so Rust ensures the exact
-- additive schema exists before Python is retired.
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
);
ALTER TABLE translation_agent_runs ADD COLUMN IF NOT EXISTS parent_agent_run_id UUID;
ALTER TABLE translation_agent_runs ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE translation_agent_runs ADD COLUMN IF NOT EXISTS checkpoint_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE translation_agent_runs ADD COLUMN IF NOT EXISTS checkpointed_at TIMESTAMPTZ;
ALTER TABLE translation_agent_runs ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(128);
ALTER TABLE translation_agent_runs ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS translation_agent_events (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES translation_agent_runs(id) ON DELETE CASCADE,
    kind VARCHAR(32) NOT NULL DEFAULT 'agent',
    action VARCHAR(128) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'ok',
    event JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_translation_agent_events_run_created
 ON translation_agent_events(run_id,created_at);
CREATE INDEX IF NOT EXISTS ix_translation_agent_events_kind_action
 ON translation_agent_events(kind,action);
CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_lease
 ON translation_agent_runs(status,lease_until);
CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_status_updated
 ON translation_agent_runs(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_parent
 ON translation_agent_runs(parent_agent_run_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_translation_agent_runs_term
 ON translation_agent_runs(target_lang,domain,normalized_term);
