-- V2-owned alerts; do not change the legacy alert_events collection.
CREATE TABLE IF NOT EXISTS vr2_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint TEXT NOT NULL UNIQUE CHECK (char_length(fingerprint) BETWEEN 1 AND 255),
    source TEXT NOT NULL CHECK (char_length(source) BETWEEN 1 AND 64),
    severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 255),
    message TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vr2_alerts_status_seen ON vr2_alerts(status,last_seen_at DESC);
CREATE INDEX IF NOT EXISTS ix_vr2_alerts_task ON vr2_alerts((details->>'task_id'));
