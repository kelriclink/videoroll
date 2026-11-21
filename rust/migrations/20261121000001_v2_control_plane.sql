-- Standalone v2 schema: v1 tables remain untouched and authoritative while migrating.
-- No v2 worker may run against the production v1 DB before a separate migration audit.
CREATE TABLE IF NOT EXISTS vr2_workflows (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    generation INTEGER NOT NULL CHECK (generation > 0),
    state TEXT NOT NULL DEFAULT 'running',
    current_stage TEXT NOT NULL DEFAULT 'download',
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(task_id, generation),
    CONSTRAINT vr2_workflow_state_check CHECK (state IN ('running','succeeded','failed','unknown','paused','canceled'))
);
CREATE INDEX IF NOT EXISTS ix_vr2_workflows_state ON vr2_workflows(state, updated_at);
CREATE TABLE IF NOT EXISTS vr2_jobs (
    id UUID PRIMARY KEY,
    workflow_id UUID NOT NULL REFERENCES vr2_workflows(id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL,
    generation INTEGER NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    external_ticket TEXT,
    last_error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vr2_job_state_check CHECK (state IN ('pending','running','awaiting','succeeded','failed','unknown','canceled')),
    CONSTRAINT vr2_stage_check CHECK(stage IN ('download','subtitle','render','dubbing','publish','cleanup')),
    UNIQUE(workflow_id, stage, generation)
);
CREATE INDEX IF NOT EXISTS ix_vr2_jobs_ready ON vr2_jobs(state, available_at, created_at);
CREATE INDEX IF NOT EXISTS ix_vr2_jobs_lease ON vr2_jobs(state, lease_until) WHERE state='running';
CREATE TABLE IF NOT EXISTS vr2_events (
    id UUID PRIMARY KEY,
    workflow_id UUID NOT NULL REFERENCES vr2_workflows(id) ON DELETE RESTRICT,
    job_id UUID REFERENCES vr2_jobs(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vr2_events_workflow ON vr2_events(workflow_id, created_at);
-- A local object is NOT automatically owned by a parent job; references are explicit.
CREATE TABLE IF NOT EXISTS vr2_objects (
    storage_key TEXT PRIMARY KEY,
    checksum_sha256 TEXT,
    size_bytes BIGINT NOT NULL CHECK(size_bytes >= 0),
    state TEXT NOT NULL DEFAULT 'live' CHECK(state IN ('live','deleting','deleted')),
    retain_until TIMESTAMPTZ NOT NULL,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vr2_object_refs (
    storage_key TEXT NOT NULL REFERENCES vr2_objects(storage_key) ON DELETE RESTRICT,
    owner_type TEXT NOT NULL,
    owner_id UUID NOT NULL,
    role TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(storage_key, owner_type, owner_id, role)
);
CREATE INDEX IF NOT EXISTS ix_vr2_objects_expiry ON vr2_objects(state, retain_until);
CREATE INDEX IF NOT EXISTS ix_vr2_refs_owner ON vr2_object_refs(owner_type, owner_id);

-- Native admin login throttling. Source keys are salted hashes, not raw client IPs.
CREATE TABLE IF NOT EXISTS vr2_login_limits (
    source_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_started TIMESTAMPTZ NOT NULL DEFAULT now(),
    blocked_until TIMESTAMPTZ
);
