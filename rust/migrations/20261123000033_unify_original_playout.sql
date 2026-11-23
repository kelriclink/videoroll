-- The original playout_asset_links is the one authoritative link table.
-- Keep original Python links intact; Rust adds staging/detach metadata on
-- the SAME business record instead of creating a parallel v2 link.
DO $preflight$
BEGIN
 IF EXISTS (SELECT 1 FROM vr2_playout_links) THEN
   RAISE EXCEPTION 'nonempty vr2_playout_links requires explicit reconcile with original playout_asset_links';
 END IF;
END $preflight$;

ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS ffplayout_channel_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS relative_media_path TEXT;
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS transfer_mode TEXT;
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS source_checksum TEXT;
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS runtime_engine TEXT NOT NULL DEFAULT 'python';
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS source_asset_id UUID;
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS playout_path TEXT;
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ready';
ALTER TABLE playout_asset_links
 ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE playout_asset_links ALTER COLUMN asset_id DROP NOT NULL;
UPDATE playout_asset_links SET source_asset_id=asset_id WHERE source_asset_id IS NULL;
-- Keep this additive column nullable: legacy Python inserts know only asset_id.
-- Rust writes it explicitly, while the compatibility view exposes asset_id as fallback.
-- Rust staging records must be writable before the actual filesystem
-- transfer mode is known. Original Python always sets transfer_mode on insert.
ALTER TABLE playout_asset_links ALTER COLUMN transfer_mode DROP NOT NULL;
ALTER TABLE playout_asset_links ADD CONSTRAINT ck_playout_canonical_status CHECK (
 status IN ('staging','ready','failed','detached') AND
 ffplayout_channel_id BETWEEN 1 AND 100000);
-- source_asset_id is an immutable audit UUID for Rust rows, deliberately not an FK.
-- The live asset_id is cleared on detach so later GC can delete the source asset.
-- Original schema supported only one channel/asset pair. Permit multiple
-- independent channels; preserve legacy per-channel relative-path uniqueness.
ALTER TABLE playout_asset_links DROP CONSTRAINT IF EXISTS uq_playout_asset_links_asset_id;
CREATE UNIQUE INDEX IF NOT EXISTS ux_playout_asset_links_asset_channel
 ON playout_asset_links(asset_id,ffplayout_channel_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_playout_asset_links_channel_path
 ON playout_asset_links(ffplayout_channel_id,relative_media_path)
 WHERE relative_media_path IS NOT NULL;

DROP TABLE vr2_playout_links;
CREATE VIEW vr2_playout_links AS
 SELECT id,task_id,asset_id,COALESCE(source_asset_id,asset_id) AS source_asset_id,
  ffplayout_channel_id AS channel_id,status,relative_media_path,
  playout_path,transfer_mode,source_checksum,reason,created_at,updated_at
 FROM playout_asset_links WHERE runtime_engine='rust-v2'
 WITH LOCAL CHECK OPTION;
COMMENT ON VIEW vr2_playout_links IS
 'Rust-facing projection of original playout_asset_links; original Python records remain in the same physical table';
