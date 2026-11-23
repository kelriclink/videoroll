-- Subtitle draft reconciliation is a child state of the original
-- Bilibili Stage=publish operation, not a second PublishJob.
-- A committed P1 remains published even if native subtitle sync is unknown.
DO $preflight$
BEGIN
 IF EXISTS(SELECT 1 FROM vr2_bilibili_subtitle_syncs) THEN
    RAISE EXCEPTION 'nonempty vr2_bilibili_subtitle_syncs requires explicit history reconciliation';
 END IF;
END $preflight$;

ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_source_key TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_source_checksum TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_aid BIGINT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_bvid TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_parts SMALLINT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_state TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_submitted JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_lease_token UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_lease_until TIMESTAMPTZ;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_inflight_cid BIGINT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_last_error TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_reference_deadline TIMESTAMPTZ NOT NULL DEFAULT (now()+interval '7 days');
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS bili_sync_created_at TIMESTAMPTZ;
ALTER TABLE operation_inbox ADD CONSTRAINT ck_rust_bili_sync CHECK (
 bili_sync_state IS NULL OR
 (runtime_engine='rust-v2' AND stage='publish'
  AND task_id IS NOT NULL AND workflow_id IS NOT NULL
  AND bili_sync_aid>0 AND bili_sync_bvid IS NOT NULL
  AND bili_sync_source_key IS NOT NULL
  AND bili_sync_parts BETWEEN 1 AND 2
  AND bili_sync_state IN ('pending','running','submitting','retry',
                          'unknown','submitted','failed')
  AND bili_sync_attempts BETWEEN 0 AND 32)
);
DROP TABLE vr2_bilibili_subtitle_syncs;
CREATE VIEW vr2_bilibili_subtitle_syncs AS
 SELECT id AS publish_job_id, task_id, workflow_id,
  bili_sync_source_key AS source_key,
  bili_sync_source_checksum AS source_checksum,
  bili_sync_aid AS aid,
  bili_sync_bvid AS bvid,
  bili_sync_parts AS expected_parts,
  bili_sync_state AS state,
  bili_sync_submitted AS submitted,
  bili_sync_attempts AS attempts,
  bili_sync_next_attempt_at AS next_attempt_at,
  bili_sync_lease_token AS lease_token,
  bili_sync_lease_until AS lease_until,
  bili_sync_inflight_cid AS inflight_cid,
  bili_sync_last_error AS last_error,
  bili_sync_reference_deadline AS reference_deadline,
  bili_sync_created_at AS created_at, updated_at
 FROM operation_inbox WHERE runtime_engine='rust-v2' AND stage='publish'
  AND bili_sync_state IS NOT NULL
 WITH LOCAL CHECK OPTION;
CREATE INDEX IF NOT EXISTS ix_original_operation_bili_sync_due
 ON operation_inbox(bili_sync_state,bili_sync_next_attempt_at)
 WHERE runtime_engine='rust-v2' AND bili_sync_state IN ('pending','retry');
CREATE INDEX IF NOT EXISTS ix_original_operation_bili_sync_lease
 ON operation_inbox(bili_sync_state,bili_sync_lease_until)
 WHERE runtime_engine='rust-v2' AND bili_sync_state IN ('running','submitting');
COMMENT ON VIEW vr2_bilibili_subtitle_syncs IS
 'Only an original operation_inbox publish-stage child view, never an independent publish or subtitle table';
