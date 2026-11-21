-- Dedicated v2 worker auth and execution state; never shares production v1 credentials.
CREATE TABLE IF NOT EXISTS vr2_render_enrollments (
    id UUID PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    label VARCHAR(128) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    worker_id UUID
);
CREATE TABLE IF NOT EXISTS vr2_render_workers (
    id UUID PRIMARY KEY,
    worker_key VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    architecture VARCHAR(32),
    version VARCHAR(64) NOT NULL DEFAULT '',
    protocol_version INTEGER NOT NULL DEFAULT 1,
    render_spec_versions JSONB NOT NULL DEFAULT '[1]'::jsonb,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    resources JSONB NOT NULL DEFAULT '{}'::jsonb,
    labels JSONB NOT NULL DEFAULT '{}'::jsonb,
    credential_hash TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'online',
    enabled BOOLEAN NOT NULL DEFAULT true,
    draining BOOLEAN NOT NULL DEFAULT false,
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vr2_worker_slots CHECK(max_concurrency BETWEEN 1 AND 32)
);
CREATE TABLE IF NOT EXISTS vr2_render_executions (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL UNIQUE REFERENCES vr2_jobs(id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    workflow_id UUID NOT NULL REFERENCES vr2_workflows(id) ON DELETE RESTRICT,
    worker_id UUID REFERENCES vr2_render_workers(id) ON DELETE RESTRICT,
    attempt INTEGER NOT NULL DEFAULT 0,
    fence_token TEXT,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','running','uploading','completed','failed','canceled')),
    mode TEXT NOT NULL DEFAULT 'burn_in',
    render_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress INTEGER NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    log_tail TEXT NOT NULL DEFAULT '',
    uploaded_asset_id UUID REFERENCES assets(id) ON DELETE RESTRICT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT vr2_render_progress CHECK(progress BETWEEN 0 AND 100)
);
CREATE INDEX IF NOT EXISTS ix_vr2_render_ready ON vr2_render_executions(state,created_at);
CREATE INDEX IF NOT EXISTS ix_vr2_render_leases ON vr2_render_executions(state,lease_until);
CREATE INDEX IF NOT EXISTS ix_vr2_render_worker ON vr2_render_executions(worker_id,state,lease_until);
