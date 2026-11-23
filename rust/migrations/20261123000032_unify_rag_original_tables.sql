-- The original translation_knowledge_items is the only persistent
-- knowledge catalog. Native Rust records use its existing UUID/content/title
-- and original pgvector column remains untouched; JSON embedding is preserved
-- only as optional native metadata, never forged into a Python pgvector.
-- Research is scoped to a Rust-owned pipeline_runs row (one 'research'
-- phase per Rust workflow), so no parallel agent-run ledger is necessary.
DO $preflight$
BEGIN
 IF EXISTS(SELECT 1 FROM vr2_knowledge_items)
 OR EXISTS(SELECT 1 FROM vr2_rag_agent_runs) THEN
   RAISE EXCEPTION 'nonempty old Rust RAG tables require explicit history reconciliation';
 END IF;
END $preflight$;

ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_collection VARCHAR(96);
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_scope TEXT;
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_task_id UUID;
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_kind TEXT;
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_source_ref TEXT;
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_language VARCHAR(16);
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_keywords JSONB;
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_embedding_json JSONB;
ALTER TABLE translation_knowledge_items ADD COLUMN IF NOT EXISTS native_retain_until TIMESTAMPTZ;
ALTER TABLE translation_knowledge_items ADD CONSTRAINT fk_knowledge_native_task
 FOREIGN KEY(native_task_id) REFERENCES tasks(id) ON DELETE RESTRICT;
ALTER TABLE translation_knowledge_items ADD CONSTRAINT ck_knowledge_native_scope CHECK(
 native_scope IS NULL OR
 (created_by='rust-v2' AND native_collection IS NOT NULL
  AND native_scope IN ('global','task')
  AND native_kind IN ('passage','term','research')
  AND ((native_scope='task' AND native_task_id IS NOT NULL)
       OR (native_scope='global' AND native_task_id IS NULL)))
);
DROP TABLE vr2_knowledge_items;
CREATE VIEW vr2_knowledge_items AS SELECT
 id,native_collection AS collection,
 native_scope AS scope,native_task_id AS task_id,native_kind AS kind,
 native_source_ref AS source_ref,title,content,native_language AS language,
 native_keywords AS keywords,native_embedding_json AS embedding,
 NULLIF(embedding_model,'') AS embedding_model,confidence::REAL AS confidence,
 native_retain_until AS retain_until,created_at,updated_at
 FROM translation_knowledge_items
 WHERE created_by='rust-v2' AND native_scope IS NOT NULL
 WITH LOCAL CHECK OPTION;
CREATE INDEX IF NOT EXISTS ix_original_knowledge_native_scope
 ON translation_knowledge_items(native_collection,native_scope,native_task_id,created_at)
 WHERE created_by='rust-v2';
CREATE INDEX IF NOT EXISTS ix_original_knowledge_native_text
 ON translation_knowledge_items USING GIN(to_tsvector('simple',content))
 WHERE created_by='rust-v2';

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_run_id UUID;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_stage TEXT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_state TEXT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_plan JSONB;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_evidence JSONB;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_result JSONB;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_error TEXT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_token_budget INTEGER NOT NULL DEFAULT 12000;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_started_at TIMESTAMPTZ;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS research_finished_at TIMESTAMPTZ;
ALTER TABLE pipeline_runs ADD CONSTRAINT ck_rust_research_run CHECK (
 research_state IS NULL OR
 (engine='rust-v2' AND research_run_id IS NOT NULL AND research_stage='research'
  AND research_state IN ('pending','running','succeeded','failed','skipped'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_research_id ON pipeline_runs(research_run_id)
 WHERE research_run_id IS NOT NULL;
DROP TABLE vr2_rag_agent_runs;
CREATE VIEW vr2_rag_agent_runs AS SELECT
 research_run_id AS id,id AS workflow_id,task_id,
 research_stage AS stage,research_state AS state,
 research_plan AS plan,research_evidence AS evidence,
 research_result AS result,research_error AS error,
 research_attempts AS attempts,research_token_budget AS token_budget,
 research_started_at AS started_at,research_finished_at AS finished_at,
 created_at,updated_at
 FROM pipeline_runs
 WHERE engine='rust-v2' AND research_state IS NOT NULL
 WITH LOCAL CHECK OPTION;
COMMENT ON VIEW vr2_knowledge_items IS
 'Rust-scoped original translation_knowledge_items, no duplicate RAG corpus';
COMMENT ON VIEW vr2_rag_agent_runs IS
 'Rust research phase columns on original pipeline_runs, not a separate agent task table';
