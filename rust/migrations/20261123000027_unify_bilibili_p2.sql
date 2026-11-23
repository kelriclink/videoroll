-- A Bilibili P2 append is extra post-publication evidence for the original
-- Rust Stage=publish operation. It is NOT another Bilibili archive PublishJob:
-- the P1 archive job and its external receipt must remain immutable.
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_dubbing_job_id UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_aid BIGINT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_output_video_key TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_title TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_state TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_attempt INTEGER;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_uploaded_filename TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_uploaded_cid BIGINT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_last_error TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_operator_evidence TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_claimed_at TIMESTAMPTZ;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_finished_at TIMESTAMPTZ;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS p2_created_at TIMESTAMPTZ;

DO $merge$
BEGIN
 IF to_regclass('public.vr2_bilibili_p2_appends') IS NULL
 OR (SELECT relkind FROM pg_class WHERE oid='public.vr2_bilibili_p2_appends'::regclass)<>'r'
 THEN
   RAISE EXCEPTION 'P2 appends must be a physical table before unification';
 END IF;
 IF EXISTS(
   SELECT 1 FROM vr2_bilibili_p2_appends a
   LEFT JOIN operation_inbox p ON p.id=a.publish_job_id
   LEFT JOIN operation_inbox d ON d.id=a.dubbing_job_id
   WHERE p.id IS NULL OR d.id IS NULL
      OR p.runtime_engine<>'rust-v2' OR d.runtime_engine<>'rust-v2'
      OR p.stage<>'publish' OR d.stage<>'dubbing'
      OR p.task_id IS DISTINCT FROM a.task_id
      OR d.task_id IS DISTINCT FROM a.task_id
      OR p.p2_state IS NOT NULL
 ) THEN
   RAISE EXCEPTION 'P2 append does not belong to matching original Rust publish/dubbing operations';
 END IF;
 UPDATE operation_inbox p SET
    p2_dubbing_job_id=a.dubbing_job_id,
    p2_aid=a.aid,
    p2_output_video_key=a.output_video_key,
    p2_title=a.title,
    p2_state=a.state,
    p2_attempt=a.attempt,
    p2_uploaded_filename=a.uploaded_filename,
    p2_uploaded_cid=a.uploaded_cid,
    p2_last_error=a.last_error,
    p2_operator_evidence=a.operator_evidence,
    p2_claimed_at=a.claimed_at,
    p2_finished_at=a.finished_at,
    p2_created_at=a.created_at
 FROM vr2_bilibili_p2_appends a WHERE p.id=a.publish_job_id;
 DROP TABLE vr2_bilibili_p2_appends;
END $merge$;

ALTER TABLE operation_inbox ADD CONSTRAINT fk_operation_p2_dubbing
 FOREIGN KEY(p2_dubbing_job_id) REFERENCES operation_inbox(id) ON DELETE RESTRICT;
ALTER TABLE operation_inbox ADD CONSTRAINT ck_original_operation_p2_append CHECK (
 p2_state IS NULL OR
 (runtime_engine='rust-v2' AND stage='publish'
  AND p2_dubbing_job_id IS NOT NULL
  AND p2_aid>0
  AND p2_output_video_key IS NOT NULL
  AND char_length(p2_title) BETWEEN 1 AND 80
  AND p2_state IN ('queued','uploading','uploaded','submitting',
                  'unknown','published','failed')
  AND p2_attempt BETWEEN 1 AND 32
  AND p2_created_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_original_operation_p2_due
 ON operation_inbox(p2_state,p2_created_at)
 WHERE runtime_engine='rust-v2' AND p2_state IN ('queued','uploaded');
CREATE INDEX IF NOT EXISTS ix_original_operation_p2_stale
 ON operation_inbox(p2_claimed_at)
 WHERE runtime_engine='rust-v2' AND p2_state IN ('uploading','submitting');

CREATE VIEW vr2_bilibili_p2_appends AS
SELECT id AS publish_job_id,task_id,
 p2_dubbing_job_id AS dubbing_job_id,
 p2_aid AS aid,
 p2_output_video_key AS output_video_key,
 p2_title AS title,
 p2_state AS state,
 p2_attempt AS attempt,
 p2_uploaded_filename AS uploaded_filename,
 p2_uploaded_cid AS uploaded_cid,
 p2_last_error AS last_error,
 p2_operator_evidence AS operator_evidence,
 p2_claimed_at AS claimed_at,
 p2_finished_at AS finished_at,
 p2_created_at AS created_at,
 updated_at
FROM operation_inbox
WHERE runtime_engine='rust-v2' AND stage='publish' AND p2_state IS NOT NULL
WITH LOCAL CHECK OPTION;
COMMENT ON VIEW vr2_bilibili_p2_appends IS
'Post-publication P2 state on original operation_inbox; P1 publish_job and external receipt are untouched.';
