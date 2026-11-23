-- These five tables represent genuinely new infrastructure capabilities
-- absent from the legacy Python schema: object GC/refs, workdir sweeper,
-- internal workflow events, and atomic authentication throttling.
-- Canonical naming makes them available to the replacement service without
-- keeping a parallel "V2 business database". Their original keys/foreign
-- keys and existing contents survive the rename.
DO $migration$
DECLARE item TEXT;
DECLARE dest TEXT;
BEGIN
 FOR item,dest IN
   SELECT * FROM (VALUES
     ('vr2_objects','storage_objects'),
     ('vr2_object_refs','storage_object_refs'),
     ('vr2_workdirs','storage_workdirs'),
     ('vr2_events','workflow_events'),
     ('vr2_login_limits','auth_login_limits')
   ) AS r(old_name,new_name)
 LOOP
   IF to_regclass('public.'||item) IS NULL
      OR (SELECT relkind FROM pg_class WHERE oid=to_regclass('public.'||item))<>'r'
   THEN RAISE EXCEPTION '% must be a physical table before canonicalization',item;
   END IF;
   IF to_regclass('public.'||dest) IS NOT NULL THEN
      RAISE EXCEPTION '% would overwrite an existing canonical relation',dest;
   END IF;
   EXECUTE format('ALTER TABLE %I RENAME TO %I',item,dest);
   -- Compatibility names are temporary automatically-updatable projections,
   -- never duplicate storage; fixture-only SQL can still inspect records.
   EXECUTE format('CREATE VIEW %I AS SELECT * FROM %I',item,dest);
 END LOOP;
END $migration$;

COMMENT ON TABLE storage_objects IS
 'Canonical independent physical media-object GC ledger, distinct from per-task assets';
COMMENT ON TABLE storage_object_refs IS
 'Explicit multi-owner lifetime references, preventing GC of active media assets';
COMMENT ON TABLE storage_workdirs IS
 'Canonical event-driven task workdir cleanup and retention records';
COMMENT ON TABLE workflow_events IS
 'Append-only workflow audit events; not the original outbox_events delivery queue';
COMMENT ON TABLE auth_login_limits IS
 'Canonical rate limiter; original security_audit_events remain event history';
