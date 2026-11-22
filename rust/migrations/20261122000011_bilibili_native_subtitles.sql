-- Independent, best-effort native Bilibili subtitle synchronization.
-- Publication state is authoritative; subtitle drafts may never alter it.
CREATE TABLE IF NOT EXISTS vr2_bilibili_subtitle_syncs (
    publish_job_id UUID PRIMARY KEY REFERENCES vr2_jobs(id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    workflow_id UUID NOT NULL REFERENCES vr2_workflows(id) ON DELETE RESTRICT,
    source_key TEXT NOT NULL,
    source_checksum TEXT,
    aid BIGINT NOT NULL CHECK (aid > 0),
    bvid TEXT NOT NULL,
    expected_parts SMALLINT NOT NULL CHECK (expected_parts BETWEEN 1 AND 2),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','running','submitting','retry','unknown','submitted','failed')),
    submitted JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 32),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    inflight_cid BIGINT,
    last_error TEXT,
    reference_deadline TIMESTAMPTZ NOT NULL DEFAULT now()+interval '7 days',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vr2_bili_subtitle_due
    ON vr2_bilibili_subtitle_syncs(state,next_attempt_at)
    WHERE state IN ('pending','retry');
CREATE INDEX IF NOT EXISTS ix_vr2_bili_subtitle_lease
    ON vr2_bilibili_subtitle_syncs(state,lease_until)
    WHERE state IN ('running','submitting');

-- Select only confirmed native receipts without scanning all historical jobs.
CREATE INDEX IF NOT EXISTS ix_vr2_bili_native_publish_receipts
    ON vr2_jobs(finished_at DESC)
    WHERE stage='publish' AND state='succeeded'
      AND result->>'native_bilibili'='true';
