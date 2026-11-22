-- Independent V2 ffplayout media links; no mutation of existing v1 playout mappings.
CREATE TABLE IF NOT EXISTS vr2_playout_links (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    asset_id UUID REFERENCES assets(id) ON DELETE SET NULL,
    source_asset_id UUID NOT NULL,
    channel_id INTEGER NOT NULL CHECK(channel_id BETWEEN 1 AND 100000),
    status TEXT NOT NULL DEFAULT 'staging'
        CHECK(status IN ('staging','ready','failed','detached')),
    relative_media_path TEXT NOT NULL,
    playout_path TEXT NOT NULL,
    transfer_mode TEXT CHECK(transfer_mode IN ('hardlink','copy')),
    source_checksum TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(asset_id,channel_id),
    UNIQUE(source_asset_id,channel_id),
    UNIQUE(channel_id,relative_media_path)
);
CREATE INDEX IF NOT EXISTS ix_vr2_playout_links_task ON vr2_playout_links(task_id,status);
CREATE INDEX IF NOT EXISTS ix_vr2_playout_links_state ON vr2_playout_links(status,updated_at);
