-- New manual-intake tasks have no workflow until explicitly started.
-- Prevent the V2 compatibility UI from accepting/mutating an unrelated V1 task.
CREATE TABLE IF NOT EXISTS vr2_managed_tasks (
    task_id UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE RESTRICT,
    origin TEXT NOT NULL CHECK(origin IN ('local_upload','youtube_manual','youtube_auto')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vr2_managed_tasks_created
    ON vr2_managed_tasks(created_at DESC);
