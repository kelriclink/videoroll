-- SAU submission intent is a durable *operation*, not a second
-- PublishJob. The original publish_jobs table still holds external SAU
-- sidecar receipts with a different UUID, and must not be duplicated or
-- mutated merely to record the coordinator's dispatch.
-- Persist the intent on original operation_inbox, which is already used for
-- all other Rust Stage Jobs. This is the only physical durable intent.
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_platform TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_account_id UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_video_key TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_cover_key TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_platform_options JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_fingerprint TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_job_id UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_external_id TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_external_url TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_dispatched_at TIMESTAMPTZ;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS sau_accepted_at TIMESTAMPTZ;

DO $merge$
BEGIN
 IF to_regclass('public.vr2_social_publish_intents') IS NULL
    OR (SELECT relkind FROM pg_class
      WHERE oid='public.vr2_social_publish_intents'::regclass)<>'r'
 THEN
   RAISE EXCEPTION 'SAU intents must be a physical table before unification';
 END IF;
 IF EXISTS(
    SELECT 1 FROM vr2_social_publish_intents s
    LEFT JOIN tasks t ON t.id=s.task_id
    WHERE t.id IS NULL OR t.runtime_owner<>'rust'
       OR s.state NOT IN ('queued','dispatching','accepted','submitted',
                          'published','unknown','failed')
 ) THEN
   RAISE EXCEPTION 'SAU intent has invalid original Rust task or unknown state';
 END IF;
 IF EXISTS(
    SELECT 1 FROM vr2_social_publish_intents s JOIN operation_inbox i
    ON i.id=s.id OR i.operation_key='v2:sau:intent:'||s.id::text
 ) THEN
   RAISE EXCEPTION 'SAU intent UUID or idempotency key conflicts with an existing operation';
 END IF;
 INSERT INTO operation_inbox(
   id,operation_key,status,runtime_engine,workflow_id,task_id,stage,generation,
   request_json,attempt_count,max_attempts,last_error,finished_at,
   created_at,updated_at,
   sau_platform,sau_account_id,sau_video_key,sau_cover_key,
   sau_platform_options,sau_fingerprint,sau_job_id,
   sau_external_id,sau_external_url,sau_dispatched_at,sau_accepted_at
 )
 SELECT
   s.id,'v2:sau:intent:'||s.id::text,s.state,'rust-v2',NULL,s.task_id,
   'sau_publish',0,
   s.metadata,s.attempt,1,s.last_error,s.finished_at,
   s.created_at,s.updated_at,
   s.platform,s.account_id,s.video_key,s.cover_key,
   s.platform_options,s.fingerprint,s.sau_job_id,
   s.external_id,s.external_url,s.dispatched_at,s.accepted_at
 FROM vr2_social_publish_intents s;
 DROP TABLE vr2_social_publish_intents;
END $merge$;

-- External SAU receipt can arrive asynchronously. sau_job_id intentionally
-- is not an FK to publish_jobs until the sidecar record actually exists.
ALTER TABLE operation_inbox ADD CONSTRAINT ck_original_operation_sau_intent CHECK (
 stage IS DISTINCT FROM 'sau_publish' OR
 (runtime_engine='rust-v2' AND workflow_id IS NULL
  AND task_id IS NOT NULL
  AND sau_platform IN ('douyin','xiaohongshu','kuaishou')
  AND sau_account_id IS NOT NULL AND sau_video_key IS NOT NULL
  AND sau_fingerprint IS NOT NULL
  AND status IN ('queued','dispatching','accepted','submitted',
                 'published','unknown','failed')
  AND attempt_count BETWEEN 1 AND 32
  AND max_attempts=1)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_original_operation_sau_target
 ON operation_inbox(task_id,sau_platform,sau_account_id)
 WHERE runtime_engine='rust-v2' AND stage='sau_publish';
CREATE UNIQUE INDEX IF NOT EXISTS ux_original_operation_sau_receipt
 ON operation_inbox(sau_job_id)
 WHERE stage='sau_publish' AND sau_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_original_operation_sau_queue
 ON operation_inbox(created_at)
 WHERE stage='sau_publish' AND status='queued';
CREATE INDEX IF NOT EXISTS ix_original_operation_sau_reconcile
 ON operation_inbox(status,updated_at)
 WHERE stage='sau_publish' AND status IN ('dispatching','accepted','unknown');

CREATE VIEW vr2_social_publish_intents AS
SELECT id,task_id,
 sau_platform AS platform,
 sau_account_id AS account_id,
 sau_video_key AS video_key,
 sau_cover_key AS cover_key,
 request_json AS metadata,
 sau_platform_options AS platform_options,
 sau_fingerprint AS fingerprint,
 status AS state,
 sau_job_id,
 sau_external_id AS external_id,
 sau_external_url AS external_url,
 last_error,
 attempt_count AS attempt,
 sau_dispatched_at AS dispatched_at,
 sau_accepted_at AS accepted_at,
 finished_at,created_at,updated_at
FROM operation_inbox
WHERE runtime_engine='rust-v2' AND stage='sau_publish'
WITH LOCAL CHECK OPTION;
COMMENT ON VIEW vr2_social_publish_intents IS
'Native SAU dispatch intent is one original operation_inbox row, not a second PublishJob; external receipt remains in publish_jobs.';
