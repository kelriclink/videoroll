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
);
CREATE INDEX IF NOT EXISTS ix_translation_memory_target_task ON translation_memory_entries(target_lang,task_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_translation_memory_target_status ON translation_memory_entries(target_lang,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_translation_memory_source_norm ON translation_memory_entries(source_norm);
