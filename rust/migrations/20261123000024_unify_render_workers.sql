-- Original Python RenderWorker registry and enrollment rows are canonical.
-- Rust V2 identity remains distinguishable by runtime_engine, not by a
-- parallel persistent table. Original vrw_ keys and vre_ enrollments survive.
ALTER TABLE render_workers
 ADD COLUMN IF NOT EXISTS runtime_engine TEXT NOT NULL DEFAULT 'python';
ALTER TABLE render_worker_enrollments
 ADD COLUMN IF NOT EXISTS runtime_engine TEXT NOT NULL DEFAULT 'python';

DO $merge$
DECLARE fk RECORD;
BEGIN
 IF to_regclass('public.vr2_render_workers') IS NULL OR
    to_regclass('public.vr2_render_enrollments') IS NULL THEN
   RAISE EXCEPTION 'missing former native RenderWorker or enrollment registry';
 END IF;
 IF (SELECT relkind FROM pg_class WHERE oid='public.vr2_render_workers'::regclass)<>'r'
    OR (SELECT relkind FROM pg_class WHERE oid='public.vr2_render_enrollments'::regclass)<>'r'
 THEN
   RAISE EXCEPTION 'former native worker registries must be physical tables';
 END IF;
 IF EXISTS (
    SELECT 1 FROM vr2_render_workers v JOIN render_workers p
       ON v.id=p.id OR v.worker_key=p.worker_key
          OR v.credential_hash=p.credential_hash
 ) THEN
   RAISE EXCEPTION 'existing Python/Rust RenderWorker identity collision; reconcile before migration';
 END IF;
 IF EXISTS (
    SELECT 1 FROM vr2_render_enrollments v JOIN render_worker_enrollments p
       ON v.id=p.id OR v.token_hash=p.token_hash
 ) THEN
   RAISE EXCEPTION 'existing Python/Rust enrollment collision; reconcile before migration';
 END IF;
 IF EXISTS (SELECT 1 FROM vr2_render_workers
            WHERE length(credential_hash)<>64 OR max_concurrency NOT BETWEEN 1 AND 32)
    OR EXISTS (SELECT 1 FROM vr2_render_enrollments WHERE length(token_hash)<>64)
 THEN
    RAISE EXCEPTION 'invalid legacy native RenderWorker hash or concurrency';
 END IF;

 INSERT INTO render_workers(
    id,worker_key,name,platform,architecture,version,protocol_version,
    render_spec_versions,capabilities,resources,labels,credential_hash,
    credential_issued_at,credential_revoked_at,status,enabled,draining,
    max_concurrency,active_jobs,last_seen_at,created_at,updated_at,runtime_engine
 )
 SELECT id,worker_key,name,platform,architecture,version,protocol_version,
    render_spec_versions,capabilities,resources,labels,credential_hash,
    created_at,NULL,status,enabled,draining,
    max_concurrency,
    (SELECT count(*)::int FROM vr2_render_executions e
        WHERE e.worker_id=w.id AND e.state IN ('running','uploading')
          AND e.lease_until>now()),
    last_seen_at,created_at,updated_at,'rust-v2'
 FROM vr2_render_workers w;

 INSERT INTO render_worker_enrollments(
    id,token_hash,label,status,expires_at,consumed_at,worker_id,created_at,runtime_engine
 )
 SELECT id,token_hash,label,
    CASE WHEN consumed_at IS NOT NULL THEN 'consumed' ELSE 'active' END,
    expires_at,consumed_at,worker_id,created_at,'rust-v2'
 FROM vr2_render_enrollments;

 FOR fk IN
   SELECT c.conrelid::regclass AS table_name,c.conname,
          string_agg(a.attname,',' ORDER BY keys.ordinality) AS col_names
   FROM pg_constraint c
   JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS keys(attnum,ordinality) ON true
   JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=keys.attnum
   WHERE c.contype='f' AND c.confrelid='public.vr2_render_workers'::regclass
   GROUP BY c.conrelid,c.conname
 LOOP
   IF fk.col_names<>'worker_id' THEN
      RAISE EXCEPTION 'unexpected native worker FK on %: %',fk.table_name,fk.col_names;
   END IF;
   EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',fk.table_name,fk.conname);
   EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY(worker_id) REFERENCES render_workers(id) ON DELETE RESTRICT',
                  fk.table_name,fk.conname);
 END LOOP;

 DROP TABLE vr2_render_enrollments;
 DROP TABLE vr2_render_workers;
END $merge$;

CREATE VIEW vr2_render_workers AS
 SELECT id,worker_key,name,platform,architecture,version,protocol_version,
        render_spec_versions,capabilities,resources,labels,credential_hash,
        status,enabled,draining,max_concurrency,last_seen_at,created_at,updated_at,
        runtime_engine
 FROM render_workers WHERE runtime_engine='rust-v2'
 WITH LOCAL CHECK OPTION;
ALTER VIEW vr2_render_workers ALTER COLUMN runtime_engine SET DEFAULT 'rust-v2';

CREATE VIEW vr2_render_enrollments AS
 SELECT id,token_hash,label,created_at,expires_at,consumed_at,worker_id,
        status,runtime_engine
 FROM render_worker_enrollments WHERE runtime_engine='rust-v2'
 WITH LOCAL CHECK OPTION;
ALTER VIEW vr2_render_enrollments ALTER COLUMN runtime_engine SET DEFAULT 'rust-v2';

COMMENT ON VIEW vr2_render_workers IS 'Scoped writable VIEW on original render_workers; no duplicate registry.';
COMMENT ON VIEW vr2_render_enrollments IS 'Scoped writable VIEW on original render_worker_enrollments; no duplicate invitations.';
