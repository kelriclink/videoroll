-- Original Python pipeline_runs is the single physical workflow ledger.
-- vr2_workflows is retained ONLY as an updatable projection for the existing
-- native scheduler while its SQL is gradually refactored. It is not a table.
-- All ids, created_at, request JSON, errors and original Hatchet rows survive.
ALTER TABLE pipeline_runs
    ADD COLUMN IF NOT EXISTS generation INTEGER,
    ADD COLUMN IF NOT EXISTS current_stage TEXT;

DO $unify$
DECLARE fk RECORD;
BEGIN
  IF to_regclass('public.vr2_workflows') IS NULL THEN
    RETURN;
  END IF;
  IF (SELECT relkind FROM pg_class WHERE oid='public.vr2_workflows'::regclass) <> 'r' THEN
    RAISE EXCEPTION 'vr2_workflows expected a physical table before workflow unification';
  END IF;
  -- Forbid collisions with the original ledger: never silently overwrite
  -- a Python/Hatchet execution with a Rust row.
  IF EXISTS(
    SELECT 1 FROM vr2_workflows w JOIN pipeline_runs p ON p.id=w.id
  ) THEN
    RAISE EXCEPTION 'workflow UUID collision between pipeline_runs and vr2_workflows';
  END IF;
  -- The old task generation is business identity. Do not renumber history.
  IF EXISTS(
    SELECT 1 FROM vr2_workflows w JOIN pipeline_runs p
      ON p.task_id=w.task_id AND p.engine='rust-v2'
     AND p.generation=w.generation
  ) THEN
    RAISE EXCEPTION 'duplicate Rust workflow generation already exists in pipeline_runs';
  END IF;
  INSERT INTO pipeline_runs(
    id,task_id,engine,workflow_name,external_run_id,state,
    request_json,error_message,generation,current_stage,
    started_at,finished_at,created_at,updated_at
  )
  SELECT id,task_id,'rust-v2','videoroll.rust.pipeline',NULL,state,
    configuration,last_error,generation,current_stage,
    created_at,
    CASE WHEN state IN ('succeeded','failed','canceled') THEN updated_at ELSE NULL END,
    created_at,updated_at
  FROM vr2_workflows;

  -- Move existing FKs to the original table, retaining their constraint
  -- names and RESTRICT delete semantics. Related job IDs do not change.
  FOR fk IN
    SELECT c.conrelid::regclass AS table_name,c.conname,
           string_agg(a.attname,',' ORDER BY keys.ordinality) AS col_names
      FROM pg_constraint c
      JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS keys(attnum,ordinality) ON true
      JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=keys.attnum
    WHERE c.contype='f' AND c.confrelid='public.vr2_workflows'::regclass
    GROUP BY c.conrelid,c.conname
  LOOP
    IF fk.col_names <> 'workflow_id' THEN
       RAISE EXCEPTION 'unexpected foreign key columns on %: %',fk.table_name,fk.col_names;
    END IF;
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',fk.table_name,fk.conname);
    EXECUTE format(
      'ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY(workflow_id) REFERENCES pipeline_runs(id) ON DELETE RESTRICT',
      fk.table_name,fk.conname
    );
  END LOOP;

  DROP TABLE vr2_workflows;
END
$unify$;

-- Do not make legacy Python runs look like Rust-owned workflows.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_runs_rust_generation
  ON pipeline_runs(task_id,generation) WHERE engine='rust-v2';

-- Query-only compatibility is insufficient because the native Rust scheduler
-- creates and updates workflows. This is an automatically updatable view;
-- its two view-local defaults fill original NOT NULL columns *only* for V2.
CREATE VIEW vr2_workflows AS
SELECT id,task_id,generation,state,current_stage,
       request_json AS configuration,error_message AS last_error,
       created_at,updated_at,engine,workflow_name
FROM pipeline_runs WHERE engine='rust-v2'
WITH LOCAL CHECK OPTION;
ALTER VIEW vr2_workflows ALTER COLUMN engine SET DEFAULT 'rust-v2';
ALTER VIEW vr2_workflows ALTER COLUMN workflow_name SET DEFAULT 'videoroll.rust.pipeline';

COMMENT ON VIEW vr2_workflows IS
'Compatibility-only updatable VIEW onto original pipeline_runs, not a second workflow storage table.';
