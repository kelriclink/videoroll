-- V2 settings are overlay-only. Existing app_settings remain immutable.
CREATE TABLE IF NOT EXISTS vr2_settings (
    key VARCHAR(128) PRIMARY KEY,
    value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vr2_settings_version CHECK(version>0)
);
