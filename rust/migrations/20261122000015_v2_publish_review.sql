-- A review belongs to an exact content+settings fingerprint. Changing
-- metadata or subtitles invalidates it; never reuse a stale AI approval.
CREATE TABLE IF NOT EXISTS vr2_publish_reviews (
    task_id UUID PRIMARY KEY REFERENCES vr2_managed_tasks(task_id) ON DELETE RESTRICT,
    fingerprint CHAR(64) NOT NULL,
    approved BOOLEAN NOT NULL,
    result JSONB NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
