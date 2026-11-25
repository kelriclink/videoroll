-- Historical append intents use the original operation ledger, not a fake
-- successful native workflow. The public id is the original publish_jobs id;
-- p2_dubbing_job_id points to the exact historical media recovery receipt.
ALTER TABLE operation_inbox DROP CONSTRAINT ck_original_operation_p2_append;
ALTER TABLE operation_inbox ADD CONSTRAINT ck_original_operation_p2_append CHECK (
 p2_state IS NULL OR (
 ((runtime_engine='rust-v2' AND stage='publish') OR
  (runtime_engine='rust-historical-p2-append' AND workflow_id IS NULL AND stage IS NULL
   AND task_id IS NOT NULL AND status='completed' AND request_json->>'publish_job_id'=id::text))
 AND p2_dubbing_job_id IS NOT NULL AND p2_aid>0 AND p2_output_video_key IS NOT NULL
 AND char_length(p2_title) BETWEEN 1 AND 80
 AND p2_state IN ('queued','uploading','uploaded','submitting','unknown','published','failed')
 AND p2_attempt BETWEEN 1 AND 32 AND p2_created_at IS NOT NULL));
CREATE OR REPLACE VIEW vr2_bilibili_p2_appends AS
SELECT id AS publish_job_id,task_id,p2_dubbing_job_id AS dubbing_job_id,p2_aid AS aid,
 p2_output_video_key AS output_video_key,p2_title AS title,p2_state AS state,p2_attempt AS attempt,
 p2_uploaded_filename AS uploaded_filename,p2_uploaded_cid AS uploaded_cid,p2_last_error AS last_error,
 p2_operator_evidence AS operator_evidence,p2_claimed_at AS claimed_at,p2_finished_at AS finished_at,
 p2_created_at AS created_at,updated_at
FROM operation_inbox WHERE p2_state IS NOT NULL AND
 ((runtime_engine='rust-v2' AND stage='publish') OR runtime_engine='rust-historical-p2-append')
WITH LOCAL CHECK OPTION;
CREATE INDEX IF NOT EXISTS ix_historical_p2_due ON operation_inbox(p2_state,p2_created_at)
 WHERE runtime_engine='rust-historical-p2-append' AND p2_state IN ('queued','uploaded');
