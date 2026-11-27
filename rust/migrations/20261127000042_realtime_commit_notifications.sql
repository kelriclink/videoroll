-- Original Python emits model events after Session.commit, not during flush.
-- PG NOTIFY is transactional: rollback emits nothing, concurrent transactions
-- deliver in commit order. Payloads are only routing IDs, never credentials,
-- requests, responses, source titles, or model outputs. No new business table.
CREATE OR REPLACE FUNCTION videoroll_ui_change_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_data jsonb;
    before_data jsonb;
    entity text;
    task text;
    topics text[];
    kind text := 'invalidate';
BEGIN
    IF TG_OP = 'DELETE' THEN row_data := to_jsonb(OLD);
    ELSE row_data := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' THEN
        before_data := to_jsonb(OLD);
        -- Pure heartbeat renewal should not cause every dashboard to reload.
        IF TG_TABLE_NAME IN ('operation_inbox','render_executions') AND
           (row_data - ARRAY['lease_until','heartbeat_at','updated_at','lease_owner']) =
           (before_data - ARRAY['lease_until','heartbeat_at','updated_at','lease_owner']) THEN
            RETURN NULL;
        END IF;
        IF row_data = before_data THEN RETURN NULL; END IF;
    END IF;
    entity := row_data->>'id';
    task := row_data->>'task_id';
    CASE TG_TABLE_NAME
      WHEN 'tasks' THEN
        task := entity;
        topics := ARRAY['tasks','queue'];
        IF TG_OP = 'DELETE' THEN kind := 'task.deleted'; END IF;
      WHEN 'assets' THEN topics := ARRAY['tasks'];
      WHEN 'subtitle_jobs' THEN topics := ARRAY['tasks','queue'];
      WHEN 'render_jobs' THEN topics := ARRAY['tasks','queue'];
      WHEN 'render_executions' THEN
        IF task IS NULL THEN
          SELECT r.task_id::text INTO task FROM render_jobs r WHERE r.id=(row_data->>'render_job_id')::uuid;
        END IF;
        topics := ARRAY['tasks','queue'];
      WHEN 'dubbing_jobs' THEN topics := ARRAY['tasks','queue'];
      WHEN 'publish_jobs' THEN topics := ARRAY['tasks','queue','publishing'];
      WHEN 'publish_batches' THEN topics := ARRAY['tasks','publishing'];
      WHEN 'accounts' THEN topics := ARRAY['publishing'];
      WHEN 'pipeline_runs' THEN topics := ARRAY['tasks','queue'];
      WHEN 'operation_inbox' THEN
        IF row_data->>'runtime_engine' = 'rust-v2' THEN
            topics := ARRAY['tasks','queue'];
            IF row_data->>'stage'='publish' THEN topics := topics || ARRAY['publishing']; END IF;
        ELSIF row_data->>'runtime_engine' LIKE 'rust-sau-%' THEN
            topics := ARRAY['publishing'];
        ELSIF row_data->>'operation_key' ~ '^youtube-download:[0-9a-fA-F-]{36}$' THEN
            task := substring(row_data->>'operation_key' from 18);
            topics := ARRAY['tasks','queue'];
        ELSE RETURN NULL;
        END IF;
      WHEN 'translation_agent_runs' THEN topics := ARRAY['agents'];
      WHEN 'translation_agent_events' THEN
        SELECT r.task_id::text INTO task FROM translation_agent_runs r WHERE r.id=(row_data->>'run_id')::uuid;
        topics := ARRAY['agents'];
      WHEN 'workflow_events' THEN
        -- Preserve agent event notifications. Ordinary workflow changes
        -- are already covered by the parent/operation/task triggers.
        IF row_data->>'kind' NOT LIKE 'agent_run.%' THEN RETURN NULL; END IF;
        SELECT p.task_id::text INTO task FROM pipeline_runs p WHERE p.id=(row_data->>'workflow_id')::uuid;
        topics := ARRAY['agents'];
      WHEN 'app_settings' THEN
        IF row_data->>'key' ~ '^task[.]title[.][0-9a-fA-F-]{36}$' THEN
            task := substring(row_data->>'key' from 12);
            topics := ARRAY['tasks'];
        ELSIF row_data->>'key' IN ('workflow.youtube_download_concurrency','subtitle.auto_profile') THEN
            topics := ARRAY['queue','resources'];
        ELSE RETURN NULL;
        END IF;
      ELSE RETURN NULL;
    END CASE;
    IF task IS NOT NULL AND task ~ '^[0-9a-fA-F-]{36}$' THEN
        topics := topics || ARRAY['task:'||task];
    END IF;
    PERFORM pg_notify('videoroll_ui_commit_v1',jsonb_build_object(
      'topics',topics,'kind',kind,'entity_id',entity)::text);
    RETURN NULL;
END;
$$;
DO $notify$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['tasks','assets','subtitle_jobs','render_jobs','render_executions',
        'dubbing_jobs','publish_jobs','publish_batches','accounts','pipeline_runs','operation_inbox',
        'translation_agent_runs','translation_agent_events','workflow_events','app_settings']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS videoroll_ui_commit ON %I',name);
        EXECUTE format('CREATE TRIGGER videoroll_ui_commit AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION videoroll_ui_change_notify()',name);
    END LOOP;
END
$notify$;
