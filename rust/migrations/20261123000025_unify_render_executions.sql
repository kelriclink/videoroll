-- One physical render_executions ledger, preserving *both* Python render_job
-- attempts and Rust operation_inbox Stage Job leases.
-- Python records remain untouched and keep their original fields/IDs.
ALTER TABLE render_executions ADD COLUMN IF NOT EXISTS runtime_engine TEXT NOT NULL DEFAULT 'python';
ALTER TABLE render_executions ADD COLUMN IF NOT EXISTS job_id UUID;
ALTER TABLE render_executions ADD COLUMN IF NOT EXISTS task_id UUID;
ALTER TABLE render_executions ADD COLUMN IF NOT EXISTS workflow_id UUID;
ALTER TABLE render_executions ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'burn_in';
ALTER TABLE render_executions ADD COLUMN IF NOT EXISTS uploaded_asset_id UUID;
-- Native queued records are intentionally unassigned and unfenced until claim.
ALTER TABLE render_executions ALTER COLUMN render_job_id DROP NOT NULL;
ALTER TABLE render_executions ALTER COLUMN worker_id DROP NOT NULL;
ALTER TABLE render_executions ALTER COLUMN fence_token DROP NOT NULL;

DO $merge$
BEGIN
 IF to_regclass('public.vr2_render_executions') IS NULL THEN
   RAISE EXCEPTION 'missing native render execution records';
 END IF;
 IF (SELECT relkind FROM pg_class WHERE oid='public.vr2_render_executions'::regclass)<>'r' THEN
   RAISE EXCEPTION 'expected native render execution physical table';
 END IF;
 IF EXISTS(
    SELECT 1 FROM vr2_render_executions n JOIN render_executions p ON n.id=p.id
 ) THEN
    RAISE EXCEPTION 'Python/Rust render execution UUID collision; reconcile before migration';
 END IF;
 IF EXISTS (
   SELECT 1 FROM vr2_render_executions e
   WHERE NOT EXISTS (SELECT 1 FROM operation_inbox o
       WHERE o.id=e.job_id AND o.runtime_engine='rust-v2')
      OR NOT EXISTS (SELECT 1 FROM pipeline_runs w
       WHERE w.id=e.workflow_id AND w.engine='rust-v2')
      OR (e.worker_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM render_workers rw
          WHERE rw.id=e.worker_id AND rw.runtime_engine='rust-v2'))
 ) THEN
   RAISE EXCEPTION 'native render execution refers to missing or non-Rust job/workflow/worker';
 END IF;
 INSERT INTO render_executions(
    id,render_job_id,job_id,task_id,workflow_id,worker_id,attempt,fence_token,
    state,mode,render_spec,capability_snapshot,transfer_mode,progress,metrics,
    log_tail,error_message,output_json,uploaded_asset_id,lease_until,heartbeat_at,
    started_at,finished_at,created_at,updated_at,runtime_engine
 )
 SELECT
    id,NULL,job_id,task_id,workflow_id,worker_id,attempt,fence_token,
    state,mode,render_spec,'{}'::jsonb,'http',progress,metrics,
    log_tail,last_error,'{}'::jsonb,uploaded_asset_id,lease_until,
    CASE WHEN state IN ('running','uploading') THEN updated_at ELSE NULL END,
    created_at,finished_at,created_at,updated_at,'rust-v2'
 FROM vr2_render_executions;
 DROP TABLE vr2_render_executions;
END $merge$;

-- Python RenderExecution still requires its historical links and fence.
-- Native execution rows must be attached to the original operation_inbox and
-- pipeline_runs. The NULLs are only allowed for the opposite engine.
ALTER TABLE render_executions ADD CONSTRAINT ck_render_exec_runtime_links
 CHECK (
    (runtime_engine='python' AND render_job_id IS NOT NULL
      AND worker_id IS NOT NULL AND fence_token IS NOT NULL)
    OR
    (runtime_engine='rust-v2' AND render_job_id IS NULL
      AND job_id IS NOT NULL AND task_id IS NOT NULL AND workflow_id IS NOT NULL)
 );
ALTER TABLE render_executions ADD CONSTRAINT fk_rust_render_job_inbox
 FOREIGN KEY(job_id) REFERENCES operation_inbox(id) ON DELETE RESTRICT;
ALTER TABLE render_executions ADD CONSTRAINT fk_rust_render_workflow
 FOREIGN KEY(workflow_id) REFERENCES pipeline_runs(id) ON DELETE RESTRICT;
ALTER TABLE render_executions ADD CONSTRAINT fk_rust_render_task
 FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT;
ALTER TABLE render_executions ADD CONSTRAINT fk_rust_render_uploaded_asset
 FOREIGN KEY(uploaded_asset_id) REFERENCES assets(id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_rust_render_execution_job_id
 ON render_executions(job_id) WHERE runtime_engine='rust-v2';
CREATE INDEX IF NOT EXISTS ix_rust_render_executions_claim
 ON render_executions(state,created_at) WHERE runtime_engine='rust-v2';
CREATE INDEX IF NOT EXISTS ix_rust_render_executions_lease
 ON render_executions(state,lease_until) WHERE runtime_engine='rust-v2';

-- Simple automatically updatable view; all native execution UPDATE/SELECT
-- statements retain their existing V2 wire shape and cannot see Python rows.
CREATE VIEW vr2_render_executions AS
 SELECT id,job_id,task_id,workflow_id,worker_id,attempt,fence_token,
        state,mode,render_spec,progress,lease_until,metrics,log_tail,
        uploaded_asset_id,error_message AS last_error,created_at,updated_at,
        finished_at,runtime_engine
 FROM render_executions WHERE runtime_engine='rust-v2'
 WITH LOCAL CHECK OPTION;
ALTER VIEW vr2_render_executions ALTER COLUMN runtime_engine SET DEFAULT 'rust-v2';
ALTER VIEW vr2_render_executions ALTER COLUMN attempt SET DEFAULT 0;
COMMENT ON VIEW vr2_render_executions IS
 'Temporary writable Rust rendering projection onto original render_executions; not a second table.';
