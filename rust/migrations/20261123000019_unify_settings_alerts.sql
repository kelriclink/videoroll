-- Canonicalize already-existing Python V1 settings and alert stores.
-- This is an additive forward migration, NOT a production cleanup script.
-- Never discard divergent V2 data: abort and require explicit reconciliation.
DO $migration$
BEGIN
    IF to_regclass('public.app_settings') IS NULL OR
       to_regclass('public.alert_events') IS NULL THEN
       RAISE EXCEPTION 'original app_settings and alert_events must exist before schema unification';
    END IF;

    IF to_regclass('public.vr2_settings') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1 FROM vr2_settings v
            JOIN app_settings a ON a.key=v.key
            WHERE a.value_json IS DISTINCT FROM v.value_json
        ) THEN
           RAISE EXCEPTION 'conflicting legacy and V2 settings; reconcile manually before unification';
        END IF;
        INSERT INTO app_settings(key,value_json,version,updated_at)
        SELECT key,value_json,version,updated_at FROM vr2_settings
        ON CONFLICT(key) DO UPDATE SET
          version=GREATEST(app_settings.version,EXCLUDED.version),
          updated_at=GREATEST(app_settings.updated_at,EXCLUDED.updated_at);
        DROP TABLE vr2_settings;
    END IF;

    IF to_regclass('public.vr2_alerts') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1 FROM vr2_alerts v JOIN alert_events a ON a.fingerprint=v.fingerprint
            WHERE (a.source,a.severity,a.status,a.title,a.message,a.details_json)
               IS DISTINCT FROM
                  (v.source,v.severity,v.status,v.title,v.message,v.details)
        ) THEN
           RAISE EXCEPTION 'conflicting legacy and V2 alert events; reconcile manually before unification';
        END IF;
        INSERT INTO alert_events(
            id,fingerprint,source,severity,status,title,message,details_json,
            occurrence_count,first_seen_at,last_seen_at,acknowledged_at,resolved_at,
            created_at,updated_at
        )
        SELECT
            id,fingerprint,source,severity,status,title,message,details,
            occurrence_count,first_seen_at,last_seen_at,acknowledged_at,resolved_at,
            first_seen_at,updated_at
        FROM vr2_alerts
        ON CONFLICT(fingerprint) DO NOTHING;
        DROP TABLE vr2_alerts;
    END IF;
END
$migration$;
