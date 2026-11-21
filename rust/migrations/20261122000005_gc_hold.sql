ALTER TABLE vr2_objects
    ADD COLUMN IF NOT EXISTS manual_hold_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_vr2_objects_hold ON vr2_objects(manual_hold_until)
    WHERE state = 'live';
