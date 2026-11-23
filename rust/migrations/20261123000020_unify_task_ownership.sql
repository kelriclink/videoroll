-- Eliminate the duplicate Rust task ownership table.
-- Python tasks default to the original engine; Rust-owned tasks carry one
-- explicit source-of-truth marker on the original tasks row.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS runtime_owner TEXT NOT NULL DEFAULT 'python';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS runtime_origin TEXT;
DO $ownership$
DECLARE fk RECORD;
BEGIN
    IF to_regclass('public.vr2_managed_tasks') IS NOT NULL THEN
        UPDATE tasks t SET runtime_owner='rust',runtime_origin=m.origin
        FROM vr2_managed_tasks m WHERE t.id=m.task_id;
        -- Redirect all existing metadata references to original tasks(id).
        FOR fk IN
          SELECT c.conrelid::regclass AS table_name,c.conname
          FROM pg_constraint c
          WHERE c.contype='f' AND c.confrelid='public.vr2_managed_tasks'::regclass
        LOOP
          EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',fk.table_name,fk.conname);
          EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT',
                          fk.table_name,fk.conname);
        END LOOP;
        DROP TABLE vr2_managed_tasks;
    END IF;
END
$ownership$;
CREATE INDEX IF NOT EXISTS ix_tasks_runtime_owner_status
    ON tasks(runtime_owner,status,updated_at);
