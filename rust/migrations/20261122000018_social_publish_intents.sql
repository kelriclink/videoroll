-- Side-effect fence for a Rust-managed task posted via the independent SAU
-- browser worker. No changes to V1 publish_jobs or V1 account secrets.
CREATE TABLE IF NOT EXISTS vr2_social_publish_intents (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    platform TEXT NOT NULL CHECK(platform IN ('douyin','xiaohongshu','kuaishou')),
    account_id UUID NOT NULL,
    video_key TEXT NOT NULL,
    cover_key TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    platform_options JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(
        state IN ('queued','dispatching','accepted','submitted','published','unknown','failed')),
    sau_job_id UUID,
    external_id TEXT,
    external_url TEXT,
    last_error TEXT,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt BETWEEN 1 AND 32),
    dispatched_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(task_id,platform,account_id)
);
CREATE INDEX IF NOT EXISTS ix_vr2_social_queue ON vr2_social_publish_intents(created_at)
  WHERE state='queued';
CREATE INDEX IF NOT EXISTS ix_vr2_social_status ON vr2_social_publish_intents(state,updated_at)
  WHERE state IN ('dispatching','accepted','unknown');
CREATE UNIQUE INDEX IF NOT EXISTS ix_vr2_social_sau_receipt
  ON vr2_social_publish_intents(sau_job_id) WHERE sau_job_id IS NOT NULL;
