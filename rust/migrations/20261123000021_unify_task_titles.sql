-- Move the original Python title settings into their one canonical store.
-- Never overwrite another title source with conflicting non-empty text.
DO $migration$
BEGIN
 IF to_regclass('public.vr2_task_titles') IS NULL THEN RETURN; END IF;
 IF EXISTS(
   SELECT 1 FROM vr2_task_titles v
   JOIN app_settings a ON a.key='task.title.'||v.task_id::text
   WHERE jsonb_typeof(a.value_json) IS DISTINCT FROM 'object'
     OR (NULLIF(trim(v.source_title),'') IS NOT NULL AND
         NULLIF(trim(a.value_json->>'source_title'),'') IS NOT NULL AND
         trim(v.source_title)<>trim(a.value_json->>'source_title'))
     OR (NULLIF(trim(v.translated_title),'') IS NOT NULL AND
         NULLIF(trim(a.value_json->>'translated_title'),'') IS NOT NULL AND
         trim(v.translated_title)<>trim(a.value_json->>'translated_title'))
 ) THEN
   RAISE EXCEPTION 'conflicting task titles between app_settings and vr2_task_titles: reconcile before migration';
 END IF;
 INSERT INTO app_settings(key,value_json,version,updated_at)
 SELECT 'task.title.'||v.task_id::text,
        jsonb_strip_nulls(jsonb_build_object(
          'source_title',NULLIF(trim(v.source_title),''),
          'translated_title',NULLIF(trim(v.translated_title),''))),
        1,v.updated_at
 FROM vr2_task_titles v
 ON CONFLICT (key) DO UPDATE SET
   value_json=app_settings.value_json || COALESCE((
     SELECT jsonb_object_agg(k.key,k.value)
     FROM jsonb_each(EXCLUDED.value_json) k
     WHERE NULLIF(trim(app_settings.value_json->>k.key),'') IS NULL
   ),'{}'::jsonb),
   version=app_settings.version+1,updated_at=now();
 DROP TABLE vr2_task_titles;
END
$migration$;
