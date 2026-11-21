-- Terminal workflow cleanup is event-driven, never a blind scan of /DATA.
CREATE TABLE IF NOT EXISTS vr2_workdirs (
    path_key TEXT PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    workflow_id UUID NOT NULL REFERENCES vr2_workflows(id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'retained'
        CHECK(state IN ('retained','deleting','deleted')),
    due_at TIMESTAMPTZ NOT NULL,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vr2_workdirs_due
    ON vr2_workdirs(state, due_at);
