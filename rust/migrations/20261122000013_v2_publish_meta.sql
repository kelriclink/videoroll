-- Immutable V1 settings and media are never overwritten.
-- Explicitly operator-approved Bilibili metadata belongs to a V2-owned task.
CREATE TABLE IF NOT EXISTS vr2_publish_meta (
    task_id UUID PRIMARY KEY REFERENCES vr2_managed_tasks(task_id) ON DELETE RESTRICT,
    value_json JSONB NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    sha256 CHAR(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Source titles are derived from owned media metadata, never written into
-- legacy task.title.* app_settings by the new Rust extractor.
CREATE TABLE IF NOT EXISTS vr2_task_titles (
    task_id UUID PRIMARY KEY REFERENCES vr2_managed_tasks(task_id) ON DELETE RESTRICT,
    source_title TEXT NOT NULL,
    translated_title TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
