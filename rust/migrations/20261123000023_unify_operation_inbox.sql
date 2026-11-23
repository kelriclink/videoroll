-- Single physical operation ledger: original Python operation_inbox.
-- Native jobs are distinguished by runtime_engine='rust-v2' rather than a
-- second durable table. The compatibility view is temporary query grammar.
-- Existing Python inbox rows, operation keys and state are never modified.

ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS runtime_engine TEXT NOT NULL DEFAULT 'python';
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS workflow_id UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS task_id UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS stage TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS generation INTEGER;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS max_attempts INTEGER DEFAULT 5;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS lease_token UUID;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS external_ticket TEXT;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE operation_inbox ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
-- A Rust exception can exceed the old Python 1024-character display cap.
-- Widening (not truncating) preserves both the Python and Rust errors.
ALTER TABLE operation_inbox ALTER COLUMN last_error TYPE TEXT;

DO $unify$
DECLARE fk RECORD;
BEGIN
 IF to_regclass('public.vr2_jobs') IS NULL THEN RETURN; END IF;
 IF (SELECT relkind FROM pg_class WHERE oid='public.vr2_jobs'::regclass)<>'r' THEN
   RAISE EXCEPTION 'vr2_jobs expected a physical table before job unification';
 END IF;
 IF EXISTS(
    SELECT 1 FROM vr2_jobs j JOIN operation_inbox o
      ON o.id=j.id OR o.operation_key=j.operation_key
 ) THEN
    RAISE EXCEPTION 'operation idempotency key or UUID collision between operation_inbox and vr2_jobs';
 END IF;
 IF EXISTS(SELECT 1 FROM vr2_jobs WHERE length(operation_key)>255) THEN
    RAISE EXCEPTION 'legacy operation key exceeds original operation_inbox limit';
 END IF;
 INSERT INTO operation_inbox(
    id,operation_key,status,request_json,result_json,lease_owner,lease_until,
    heartbeat_at,attempt_count,retry_after,recovery_count,restart_token,
    last_error,completed_at,created_at,updated_at,
    runtime_engine,workflow_id,task_id,stage,generation,max_attempts,
    lease_token,available_at,external_ticket,started_at,finished_at
 )
 SELECT
    id,operation_key,state,payload,result,NULL,lease_until,
    CASE WHEN state='running' THEN updated_at ELSE NULL END,
    attempts,available_at,0,NULL,
    last_error,finished_at,created_at,updated_at,
    'rust-v2',workflow_id,task_id,stage,generation,max_attempts,
    lease_token,available_at,external_ticket,started_at,finished_at
 FROM vr2_jobs;

 FOR fk IN
   SELECT c.conrelid::regclass AS table_name,c.conname,
          string_agg(a.attname,',' ORDER BY keys.ordinality) AS col_names
   FROM pg_constraint c
   JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS keys(attnum,ordinality) ON true
   JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=keys.attnum
   WHERE c.contype='f' AND c.confrelid='public.vr2_jobs'::regclass
   GROUP BY c.conrelid,c.conname
 LOOP
   IF fk.col_names NOT IN ('job_id','publish_job_id','dubbing_job_id') THEN
      RAISE EXCEPTION 'unexpected job FK columns on %: %',fk.table_name,fk.col_names;
   END IF;
   EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',fk.table_name,fk.conname);
   EXECUTE format(
      'ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY(%I) REFERENCES operation_inbox(id) ON DELETE RESTRICT',
      fk.table_name,fk.conname,fk.col_names);
 END LOOP;

 DROP TABLE vr2_jobs;
END
$unify$;

-- Original Python Inbox rows have NULL workflow_id, so this FK does not
-- change Python business behavior.
ALTER TABLE operation_inbox
  ADD CONSTRAINT fk_rust_operation_workflow
  FOREIGN KEY(workflow_id) REFERENCES pipeline_runs(id) ON DELETE RESTRICT;
ALTER TABLE operation_inbox
  ADD CONSTRAINT fk_rust_operation_task
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_operation_inbox_rust_workflow_stage
 ON operation_inbox(workflow_id,stage,generation) WHERE runtime_engine='rust-v2';
CREATE INDEX IF NOT EXISTS ix_operation_inbox_rust_ready
 ON operation_inbox(status,available_at,created_at) WHERE runtime_engine='rust-v2';
CREATE INDEX IF NOT EXISTS ix_operation_inbox_rust_lease
 ON operation_inbox(status,lease_until) WHERE runtime_engine='rust-v2' AND status='running';

-- Simple automatically updatable view; updates are fenced by CHECK OPTION.
-- Python operation_inbox rows stay invisible to the native Stage scheduler.
CREATE VIEW vr2_jobs AS
 SELECT id,workflow_id,task_id,stage,generation,operation_key,
        status AS state,request_json AS payload,result_json AS result,
        attempt_count AS attempts,max_attempts,lease_token,lease_until,
        available_at,external_ticket,last_error,started_at,finished_at,
        created_at,updated_at,runtime_engine
 FROM operation_inbox WHERE runtime_engine='rust-v2'
 WITH LOCAL CHECK OPTION;
ALTER VIEW vr2_jobs ALTER COLUMN runtime_engine SET DEFAULT 'rust-v2';

COMMENT ON VIEW vr2_jobs IS
'Compatibility-only updatable VIEW onto original operation_inbox; Stage scheduling data is not duplicated.';
