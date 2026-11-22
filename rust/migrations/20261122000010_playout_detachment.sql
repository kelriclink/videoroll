-- Existing V2 databases may already have migration 09 with a RESTRICT FK.
-- Keep an audit-only original UUID, release the live asset reference on GC.
ALTER TABLE vr2_playout_links
    ADD COLUMN IF NOT EXISTS source_asset_id UUID;
UPDATE vr2_playout_links SET source_asset_id=asset_id
    WHERE source_asset_id IS NULL AND asset_id IS NOT NULL;
ALTER TABLE vr2_playout_links
    ALTER COLUMN source_asset_id SET NOT NULL;
ALTER TABLE vr2_playout_links
    DROP CONSTRAINT IF EXISTS vr2_playout_links_asset_id_fkey;
ALTER TABLE vr2_playout_links
    ALTER COLUMN asset_id DROP NOT NULL;
ALTER TABLE vr2_playout_links
    ADD CONSTRAINT vr2_playout_links_asset_id_fkey
        FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_vr2_playout_original_channel
    ON vr2_playout_links(source_asset_id,channel_id);
