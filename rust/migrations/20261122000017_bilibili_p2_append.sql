-- One append-only intent per confirmed native Bilibili P1. Separate from the
-- original published job; never modify or rerun the original archive POST.
CREATE TABLE IF NOT EXISTS vr2_bilibili_p2_appends (
    publish_job_id UUID PRIMARY KEY REFERENCES vr2_jobs(id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    dubbing_job_id UUID NOT NULL REFERENCES vr2_jobs(id) ON DELETE RESTRICT,
    aid BIGINT NOT NULL CHECK(aid > 0),
    output_video_key TEXT NOT NULL,
    title TEXT NOT NULL CHECK(char_length(title) BETWEEN 1 AND 80),
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','uploading','uploaded','submitting','unknown','published','failed')),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 32),
    uploaded_filename TEXT,
    uploaded_cid BIGINT,
    last_error TEXT,
    operator_evidence TEXT,
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vr2_bili_p2_due ON vr2_bilibili_p2_appends(state,created_at)
  WHERE state IN ('queued','uploaded');
CREATE INDEX IF NOT EXISTS ix_vr2_bili_p2_stale ON vr2_bilibili_p2_appends(claimed_at)
  WHERE state IN ('uploading','submitting');
