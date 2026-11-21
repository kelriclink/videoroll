-- Minimal v1-compatible base schema ONLY for a disposable integration-test DB.
CREATE TYPE source_type AS ENUM ('youtube','local','url');
CREATE TYPE source_license AS ENUM ('own','authorized','cc','unknown');
CREATE TYPE task_status AS ENUM ('CREATED','INGESTED','DOWNLOADED','AUDIO_EXTRACTED','ASR_DONE','TRANSLATED','SUBTITLE_READY','RENDERED','READY_FOR_REVIEW','APPROVED','PUBLISHING','PUBLISHED','FAILED','CANCELED');
CREATE TYPE subtitle_format AS ENUM ('srt','vtt','ass');
CREATE TYPE asset_kind AS ENUM ('video_raw','metadata_json','audio_wav','segments_json','subtitle_srt','subtitle_ass','video_final','dubbing_audio','video_dubbed','cover_image','log','publish_result');
CREATE TABLE tasks (
  id UUID PRIMARY KEY,
  source_type source_type NOT NULL,
  source_url TEXT,
  source_license source_license NOT NULL,
  status task_status NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  origin_kind VARCHAR(32),
  origin_run_id VARCHAR(64),
  restart_generation INTEGER NOT NULL DEFAULT 0,
  created_by VARCHAR(128),
  error_code VARCHAR(64),
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE ingested_videos (
  id UUID PRIMARY KEY, platform VARCHAR(32) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  task_id UUID NOT NULL REFERENCES tasks(id),
  UNIQUE(platform,source_id)
);
CREATE TABLE assets (
  id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id),
  kind asset_kind NOT NULL, storage_key TEXT NOT NULL,
  sha256 TEXT,size_bytes BIGINT, duration_ms BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE subtitles (
  id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id),
  version INTEGER NOT NULL DEFAULT 1,format subtitle_format NOT NULL,
  language VARCHAR(16) NOT NULL DEFAULT 'zh',
  storage_key TEXT NOT NULL
);

CREATE TABLE app_settings (
  key VARCHAR(255) PRIMARY KEY,
  value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE playout_asset_links (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES tasks(id),
  asset_id UUID NOT NULL REFERENCES assets(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TYPE youtube_source_type AS ENUM ('channel','playlist');
CREATE TYPE youtube_source_license AS ENUM ('own','authorized','cc','unknown');
CREATE TABLE youtube_sources (
  id UUID PRIMARY KEY,
  source_type youtube_source_type NOT NULL,
  source_id VARCHAR(128) NOT NULL,
  source_url TEXT,
  display_name VARCHAR(255),
  license youtube_source_license NOT NULL DEFAULT 'unknown',
  proof_url TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  scan_interval_minutes INTEGER NOT NULL DEFAULT 60,
  scan_limit INTEGER NOT NULL DEFAULT 20,
  auto_process BOOLEAN NOT NULL DEFAULT true,
  last_scan_started_at TIMESTAMPTZ,
  last_scan_finished_at TIMESTAMPTZ,
  last_scan_discovered_count INTEGER NOT NULL DEFAULT 0,
  last_scan_created_count INTEGER NOT NULL DEFAULT 0,
  last_scan_started_pipeline_count INTEGER NOT NULL DEFAULT 0,
  last_scan_skipped_duplicates INTEGER NOT NULL DEFAULT 0,
  last_scan_error TEXT,
  last_nonempty_scan_finished_at TIMESTAMPTZ,
  last_nonempty_discovered_count INTEGER NOT NULL DEFAULT 0,
  total_scan_count INTEGER NOT NULL DEFAULT 0,
  total_failed_scan_count INTEGER NOT NULL DEFAULT 0,
  total_created_count INTEGER NOT NULL DEFAULT 0,
  total_started_pipeline_count INTEGER NOT NULL DEFAULT 0,
  scan_lock_owner VARCHAR(128),
  scan_lock_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(source_type,source_id)
);
