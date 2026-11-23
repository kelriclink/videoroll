-- Native YouDub result proof belongs to the durable Stage Job in the
-- original operation_inbox. Do not manufacture a Python DubbingJob, which
-- has a different independent lease/state machine and its own UUID.
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS dubbing_source_video_key TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS dubbing_source_srt_key TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS dubbing_output_video_key TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS dubbing_model_job_id UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS dubbing_model_status TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS dubbing_run_created_at TIMESTAMPTZ;
DO $merge$
BEGIN
 IF to_regclass('public.vr2_dubbing_runs') IS NULL
    OR (SELECT relkind FROM pg_class WHERE oid='public.vr2_dubbing_runs'::regclass)<>'r' THEN
   RAISE EXCEPTION 'vr2_dubbing_runs must be a physical table before unification';
 END IF;
 IF EXISTS (
    SELECT 1 FROM vr2_dubbing_runs r
    LEFT JOIN operation_inbox j ON j.id=r.job_id
    WHERE j.id IS NULL OR j.runtime_engine<>'rust-v2' OR j.stage<>'dubbing'
       OR j.workflow_id IS DISTINCT FROM r.workflow_id
       OR j.task_id IS DISTINCT FROM r.task_id
       OR j.dubbing_model_status IS NOT NULL
 ) THEN
   RAISE EXCEPTION 'dubbing run does not match an untouched native dubbing operation';
 END IF;
 UPDATE operation_inbox j SET
    dubbing_source_video_key=r.source_video_key,
    dubbing_source_srt_key=r.source_srt_key,
    dubbing_output_video_key=r.output_video_key,
    dubbing_model_job_id=r.model_job_id,
    dubbing_model_status=r.model_status,
    dubbing_run_created_at=r.created_at
 FROM vr2_dubbing_runs r WHERE j.id=r.job_id;
 DROP TABLE vr2_dubbing_runs;
END $merge$;

ALTER TABLE operation_inbox ADD CONSTRAINT ck_operation_dubbing_proof CHECK(
 dubbing_model_status IS NULL OR
 (runtime_engine='rust-v2' AND stage='dubbing' AND
  dubbing_model_status='succeeded' AND
  dubbing_source_video_key IS NOT NULL AND
  dubbing_source_srt_key IS NOT NULL AND
  dubbing_output_video_key IS NOT NULL AND
  dubbing_model_job_id IS NOT NULL AND
  dubbing_run_created_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_original_operation_dubbing_run_media
 ON operation_inbox(workflow_id,dubbing_output_video_key)
 WHERE runtime_engine='rust-v2' AND dubbing_model_status='succeeded';

CREATE VIEW vr2_dubbing_runs AS
SELECT id AS job_id,workflow_id,task_id,
 dubbing_source_video_key AS source_video_key,
 dubbing_source_srt_key AS source_srt_key,
 dubbing_output_video_key AS output_video_key,
 dubbing_model_job_id AS model_job_id,
 dubbing_model_status AS model_status,
 dubbing_run_created_at AS created_at
FROM operation_inbox
WHERE runtime_engine='rust-v2' AND stage='dubbing'
  AND dubbing_model_status='succeeded'
WITH LOCAL CHECK OPTION;
COMMENT ON VIEW vr2_dubbing_runs IS
 'Read-only compatibility projection of YouDub completed stage evidence in original operation_inbox; no separate records.';
