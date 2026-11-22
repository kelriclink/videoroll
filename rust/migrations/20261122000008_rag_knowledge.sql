-- Task-scoped retrieval and research notes. Separate from v1 direct SQL and
-- optional pgvector, allowing deterministic fallback when embeddings are off.
CREATE TABLE IF NOT EXISTS vr2_knowledge_items (
    id UUID PRIMARY KEY,
    collection VARCHAR(96) NOT NULL DEFAULT 'translation',
    scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global','task')),
    task_id UUID REFERENCES tasks(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL DEFAULT 'passage' CHECK(kind IN ('passage','term','research')),
    source_ref TEXT,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    language VARCHAR(16) NOT NULL DEFAULT 'en',
    keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding JSONB,
    embedding_model TEXT,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
    retain_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK((scope='task' AND task_id IS NOT NULL) OR (scope='global' AND task_id IS NULL)),
    CHECK(length(content) BETWEEN 1 AND 20000)
);
CREATE INDEX IF NOT EXISTS ix_vr2_knowledge_scope
    ON vr2_knowledge_items(collection,scope,task_id,created_at);
CREATE INDEX IF NOT EXISTS ix_vr2_knowledge_lexical
    ON vr2_knowledge_items USING GIN(to_tsvector('simple',content));
CREATE TABLE IF NOT EXISTS vr2_rag_agent_runs (
    id UUID PRIMARY KEY,
    workflow_id UUID NOT NULL REFERENCES vr2_workflows(id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL DEFAULT 'research',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','running','succeeded','failed','skipped')),
    plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    token_budget INTEGER NOT NULL DEFAULT 12000,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workflow_id,stage)
);
CREATE INDEX IF NOT EXISTS ix_vr2_rag_agent_workflow
    ON vr2_rag_agent_runs(workflow_id,state);
